#!/usr/bin/env python
"""Write an Experience Builder edit to BOTH config surfaces, then read both back.

A published Experience Builder app keeps its configuration twice. The item data
(.../content/items/<id>/data) is the published copy that visitors load. The
resource resources/config/config.json is the builder's draft. Fix one of them
over REST and the app looks right until somebody opens the builder and presses
Publish: the draft overwrites the item data and the fix is gone, with no error.

This tool applies a list of replace-only edits to both surfaces. It refuses
before writing when either surface does not hold the value the edit expects,
when the two surfaces disagree in structure (unpublished builder work), or when
the item is not a Web Experience. It writes the draft first and the published
copy second, so a crash between the two leaves the harmless state (a draft
ahead of the published copy), never the silent one (a published fix with a
stale draft). It then re-reads both surfaces with the CDN cache defeated and
exits non-zero unless every edited value reads back and nothing else moved.

    python exbtwin.py --self-test
    python exbtwin.py --portal https://example.maps.arcgis.com --item <32 hex> --edits edits.json
    python exbtwin.py --portal ... --item ... --edits edits.json --token-file t.txt \\
        --backup-dir ./backup --apply

An edits file is a JSON list. Each edit names a JSON Pointer (RFC 6901), the
value it expects to replace and the new value. Rich-text markup legitimately
differs between the surfaces, so expect_data, expect_draft, value_data and
value_draft override expect and value for one surface:

    [{"path": "/widgets/widget_1/config/title",
      "expect": "Old title", "value": "New title"}]

Nothing is written without --apply. The token comes from --token-file or the
ARCGIS_TOKEN environment variable, never from a flag.

Exit codes: 0 done or check clean, 1 refused, 2 a write failed, 3 written but
not verified, 4 the app could not be read, 64 usage error.
"""

import argparse
import collections
import contextlib
import copy
import http.server
import io
import json
import os
import re
import runpy
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# The item type Esri documents for an app made in ArcGIS Experience Builder.
ITEM_TYPE = "Web Experience"

# The builder's draft lives in this item resource. Prefix and file name are the
# two halves updateResources wants.
DRAFT_PREFIX = "config"
DRAFT_FILE = "config.json"

# Read-backs after a write. The draft resource sits behind a CDN, so the first
# read can return the old copy. Each retry waits DEFAULT_RETRY_DELAY seconds.
DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 2.0
DEFAULT_TIMEOUT = 30.0

# Hosts that may be reached over plain http (the self-test stub). Everything
# else must use https, because the token travels in the request.
LOCAL_HOSTS = ("localhost", "127.0.0.1")

# How many paths a refusal lists before it says "and N more".
LIST_LIMIT = 5

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_WRITE_FAILED = 2
EXIT_UNVERIFIED = 3
EXIT_READ_FAILED = 4
EXIT_USAGE = 64

SURFACES = ("data", "draft")
WRITE_ORDER = ("draft", "data")        # draft first: see the module docstring
EDIT_KEYS = frozenset(["path", "expect", "expect_data", "expect_draft",
                       "value", "value_data", "value_draft"])
BACKUP_FILES = ("data.json", "draft.json", "inverse.json")

_ITEM_ID = re.compile(r"^[0-9a-fA-F]{32}\Z")
_INDEX = re.compile(r"^(0|[1-9][0-9]*)\Z")

Decision = collections.namedtuple("Decision", "problems notes states docs")


class PortalError(Exception):
    """A request failed or the portal answered with an error."""


# ------------------------------------------------------------ pure: values

def same(a, b):
    """JSON equality that tells 1, 1.0 and true apart and ignores key order."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _short(value, limit=60):
    text = json.dumps(value)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def parse_pointer(path):
    """Split an RFC 6901 JSON Pointer into its parts. Empty parts are refused."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("path must be a JSON Pointer starting with /")
    parts = tuple(p.replace("~1", "/").replace("~0", "~")
                  for p in path[1:].split("/"))
    if "" in parts:
        raise ValueError("path has an empty segment: %s" % path)
    return parts


def format_pointer(parts):
    return "/" + "/".join(p.replace("~", "~0").replace("/", "~1")
                          for p in parts)


def _paths(paths):
    shown = ", ".join(format_pointer(p) if p else "(root)"
                      for p in paths[:LIST_LIMIT])
    extra = len(paths) - LIST_LIMIT
    return shown + (" and %d more" % extra if extra > 0 else "")


def get_path(doc, parts):
    """Return (found, value) for a parts tuple. Never raises."""
    node = doc
    for part in parts:
        if isinstance(node, dict):
            if part not in node:
                return False, None
            node = node[part]
        elif (isinstance(node, list) and _INDEX.match(part)
              and int(part) < len(node)):
            node = node[int(part)]
        else:
            return False, None
    return True, node


def set_path(doc, parts, value):
    """Return a copy of doc with the EXISTING path replaced. Callers check first."""
    new = copy.deepcopy(doc)
    node = new
    for part in parts[:-1]:
        node = node[part] if isinstance(node, dict) else node[int(part)]
    if isinstance(node, dict):
        node[parts[-1]] = value
    else:
        node[int(parts[-1])] = value
    return new


# ------------------------------------------------------------ pure: edits

def parse_edits(text):
    """Turn an edits file into a list of normalised edits, or raise ValueError."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ValueError("the edits file is not JSON: %s" % exc)
    if not isinstance(raw, list) or not raw:
        raise ValueError("the edits file must be a non-empty JSON list")
    edits = []
    for number, item in enumerate(raw, 1):
        label = "edit %d" % number
        if not isinstance(item, dict):
            raise ValueError("%s is not an object" % label)
        unknown = sorted(set(item) - EDIT_KEYS)
        if unknown:
            raise ValueError("%s has unknown key(s): %s"
                             % (label, ", ".join(unknown)))
        if "path" not in item:
            raise ValueError("%s has no path" % label)
        try:
            parts = parse_pointer(item["path"])
        except ValueError as exc:
            raise ValueError("%s: %s" % (label, exc))
        expect, value = {}, {}
        for surface in SURFACES:
            for name, dest in (("expect", expect), ("value", value)):
                if name + "_" + surface in item:
                    dest[surface] = item[name + "_" + surface]
                elif name in item:
                    dest[surface] = item[name]
                else:
                    raise ValueError("%s has no %s for the %s surface"
                                     % (label, name, surface))
        edits.append({"path": item["path"], "parts": parts,
                      "expect": expect, "value": value})
    for i, first in enumerate(edits):
        for second in edits[i + 1:]:
            n = min(len(first["parts"]), len(second["parts"]))
            if first["parts"][:n] == second["parts"][:n]:
                raise ValueError("edits %s and %s overlap, so their order "
                                 "would matter" % (first["path"], second["path"]))
    return edits


def inverse_edits(edits):
    """The edits that undo these ones, in the file format parse_edits reads."""
    out = []
    for edit in edits:
        item = {"path": edit["path"]}
        for surface in SURFACES:
            item["expect_" + surface] = edit["value"][surface]
            item["value_" + surface] = edit["expect"][surface]
        out.append(item)
    return out


# ------------------------------------------------------------ pure: decide

def plan_surface(doc, edits, surface):
    """Return (state, problems, new_doc) for one surface.

    state is "applied" when every edit already holds, which is how a rerun
    after a half finished run completes instead of failing.
    """
    problems = []
    applied = 0
    new = doc
    for edit in edits:
        found, current = get_path(doc, edit["parts"])
        want = edit["value"][surface]
        if not found:
            problems.append("%s: %s is absent" % (surface, edit["path"]))
        elif same(current, want):
            applied += 1
        elif not same(current, edit["expect"][surface]):
            problems.append("%s: %s holds %s but the edit expected %s"
                            % (surface, edit["path"], _short(current),
                               _short(edit["expect"][surface])))
        else:
            new = set_path(new, edit["parts"], want)
    state = "applied" if applied == len(edits) else "apply"
    return state, problems, new


def diff_docs(a, b, skip):
    """Return (hard, text): where two documents differ outside the skip paths.

    hard: keys or list lengths differ, or a non-text value differs.
    text: both sides are strings and the strings differ. The builder rewrites
    rich-text markup when it saves the draft, so text alone is not drift.
    """
    hard, text = [], []

    def walk(x, y, path):
        if path in skip:
            return
        if isinstance(x, dict) and isinstance(y, dict):
            for key in sorted(set(x) ^ set(y)):
                hard.append(path + (key,))
            for key in sorted(set(x) & set(y)):
                walk(x[key], y[key], path + (key,))
        elif isinstance(x, list) and isinstance(y, list):
            if len(x) != len(y):
                hard.append(path)
            else:
                for index, (p, q) in enumerate(zip(x, y)):
                    walk(p, q, path + (str(index),))
        elif isinstance(x, str) and isinstance(y, str):
            if x != y:
                text.append(path)
        elif not same(x, y):
            hard.append(path)

    walk(a, b, ())
    return hard, text


def decide(info, data, draft, edits, allow_divergent=False):
    """Decide whether the edits may be written. Pure: no network, no files."""
    problems, notes, states, docs = [], [], {}, {}
    if info.get("type") != ITEM_TYPE:
        problems.append("the item type is %s, not %s"
                        % (_short(info.get("type")), ITEM_TYPE))
    if not isinstance(info.get("owner"), str) or not info["owner"]:
        problems.append("the item reports no owner, so the write URL cannot "
                        "be built")
    if problems:
        return Decision(problems, notes, states, docs)
    skip = set(edit["parts"] for edit in edits)
    for surface, doc in (("data", data), ("draft", draft)):
        state, found, new = plan_surface(doc, edits, surface)
        problems.extend(found)
        states[surface] = state
        docs[surface] = new
    hard, text = diff_docs(data, draft, skip)
    if hard:
        message = ("the draft and the published copy differ in structure at "
                   "%s, which means unpublished builder work; the next "
                   "Publish would ship it" % _paths(hard))
        if allow_divergent:
            notes.append(message)
        else:
            problems.append(message + ". Review it, then pass "
                            "--allow-divergent to proceed")
    if text:
        notes.append("%d text value(s) differ between the surfaces outside "
                     "the edits; the builder rewrites markup, so this is not "
                     "treated as drift" % len(text))
    return Decision(problems, notes, states, docs)


def verify_surface(surface, expected, actual, edits):
    """Compare a re-read surface with what was written. Returns (problems, notes)."""
    problems, notes = [], []
    for edit in edits:
        found, current = get_path(actual, edit["parts"])
        if not found or not same(current, edit["value"][surface]):
            problems.append("%s: %s reads back %s, wanted %s"
                            % (surface, edit["path"],
                               _short(current) if found else "(absent)",
                               _short(edit["value"][surface])))
    hard, text = diff_docs(expected, actual,
                           set(edit["parts"] for edit in edits))
    if hard:
        problems.append("%s: content changed outside the edits at %s"
                        % (surface, _paths(hard)))
    if text:
        notes.append("%s: %d text value(s) outside the edits were rewritten "
                     "by the portal" % (surface, len(text)))
    return problems, notes


# ------------------------------------------------------------ pure: input

def check_item_id(value):
    return bool(_ITEM_ID.match(value))


def check_portal(url):
    """Return an error string for an unusable portal URL, else None."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
    except ValueError:
        return "the portal URL is not a valid URL"
    if parsed.scheme not in ("https", "http") or not host:
        return "the portal must be an https URL with a host"
    if parsed.scheme == "http" and host not in LOCAL_HOSTS:
        return "the portal must use https; the token travels in the request"
    if (parsed.username or parsed.password or parsed.query
            or parsed.fragment or parsed.params):
        return "the portal URL must hold only a scheme, host and path"
    return None


def load_token(token_file, environ):
    """Return the token from a file, else ARCGIS_TOKEN, else an empty string."""
    if token_file:
        with io.open(token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
        if not token:
            raise ValueError("the token file is empty")
        return token
    return environ.get("ARCGIS_TOKEN", "").strip()


def usage_problem(args, token):
    """Return the first usage error for parsed arguments, else None."""
    if not (args.portal and args.item and args.edits):
        return "--portal, --item and --edits are all required."
    if not check_item_id(args.item):
        return "--item must be the 32 character hex item id."
    bad = check_portal(args.portal)
    if bad:
        return bad + "."
    if args.retries < 1 or args.retry_delay < 0 or args.timeout <= 0:
        return ("--retries must be at least 1, --retry-delay cannot be "
                "negative and --timeout must be positive.")
    if args.apply and not args.backup_dir:
        return "--apply needs --backup-dir, so the old copies are kept."
    if args.apply and not token:
        return ("--apply needs a token: use --token-file or set "
                "ARCGIS_TOKEN.")
    return None


# ------------------------------------------------------------ portal I/O

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so the token only reaches the host that was named."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Portal(object):
    """The few ArcGIS REST calls this tool needs. Every call sends f=json."""

    def __init__(self, root, token="", timeout=DEFAULT_TIMEOUT):
        self.root = root.rstrip("/")
        self.base = self.root + "/sharing/rest/"
        self.token = token
        self.timeout = timeout
        self.calls = 0
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _clean(self, text):
        return text.replace(self.token, "***") if self.token else text

    def call(self, path, fields=None):
        """GET when fields is None, else POST them. Returns the parsed JSON."""
        self.calls += 1
        form = dict(fields or {})
        form["f"] = "json"
        if self.token:
            form["token"] = self.token
        headers = {"Cache-Control": "no-cache", "Referer": self.root}
        if fields is None:
            # A throwaway parameter defeats the CDN that fronts item resources.
            form["_ts"] = "%d%d" % (time.time() * 1000, self.calls)
            request = urllib.request.Request(
                self.base + path + "?" + urllib.parse.urlencode(form),
                headers=headers)
        else:
            request = urllib.request.Request(
                self.base + path,
                data=urllib.parse.urlencode(form).encode("ascii"),
                headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read()
        except OSError as exc:
            raise PortalError(self._clean("%s: %s" % (path, exc)))
        try:
            doc = json.loads(body.decode("utf-8"))
        except ValueError:
            raise PortalError("%s did not return JSON" % path)
        if isinstance(doc, dict) and isinstance(doc.get("error"), dict):
            error = doc["error"]
            raise PortalError(self._clean("%s: portal error %s %s" % (
                path, error.get("code"), error.get("message"))))
        return doc


def surface_path(item_id, surface):
    if surface == "data":
        return "content/items/%s/data" % item_id
    return "content/items/%s/resources/%s/%s" % (item_id, DRAFT_PREFIX,
                                                 DRAFT_FILE)


def read_surface(portal, item_id, surface):
    doc = portal.call(surface_path(item_id, surface))
    if not isinstance(doc, dict):
        raise PortalError("the %s surface is not a JSON object" % surface)
    return doc


def read_all(portal, item_id):
    """Return (info, data, draft) as read right now."""
    info = portal.call("content/items/%s" % item_id)
    if not isinstance(info, dict):
        raise PortalError("the item record is not a JSON object")
    return (info, read_surface(portal, item_id, "data"),
            read_surface(portal, item_id, "draft"))


def write_surface(portal, item_id, info, surface, doc):
    """Write one whole surface. Raises PortalError unless the portal says success."""
    owner = urllib.parse.quote(info["owner"], safe="")
    text = json.dumps(doc, separators=(",", ":"))
    if surface == "data":
        folder = info.get("ownerFolder")
        where = owner + "/" + urllib.parse.quote(str(folder), safe="") \
            if folder else owner
        answer = portal.call("content/users/%s/items/%s/update"
                             % (where, item_id), {"text": text})
    else:
        answer = portal.call("content/users/%s/items/%s/updateResources"
                             % (owner, item_id),
                             {"fileName": DRAFT_FILE,
                              "resourcesPrefix": DRAFT_PREFIX, "text": text})
    if not (isinstance(answer, dict) and answer.get("success") is True):
        raise PortalError("the %s write did not report success" % surface)


def write_backup(folder, data, draft, edits):
    """Keep both old copies and the undo edits. Returns [] or the clashing names."""
    clash = [n for n in BACKUP_FILES
             if os.path.exists(os.path.join(folder, n))]
    if clash:
        return clash
    os.makedirs(folder, exist_ok=True)
    for name, doc in zip(BACKUP_FILES,
                         (data, draft, inverse_edits(edits))):
        with io.open(os.path.join(folder, name), "w", encoding="utf-8") as out:
            json.dump(doc, out, indent=1)
    return []


def roll_back(portal, item_id, info, done, originals, say):
    """Put already written surfaces back, then read each one to confirm."""
    for surface in reversed(done):
        try:
            write_surface(portal, item_id, info, surface, originals[surface])
            back = read_surface(portal, item_id, surface)
        except PortalError as exc:
            say("  rollback of %s FAILED: %s. Restore it from the backup."
                % (surface, exc))
            continue
        if same(back, originals[surface]):
            say("  rolled back %s and read it back: restored" % surface)
        else:
            say("  rolled back %s but the read-back does not match. Restore "
                "it from the backup." % surface)


# ------------------------------------------------------------ the run

def execute(portal, item_id, edits, apply_, backup_dir, allow_divergent,
            retries, delay, say):
    """Check, and with apply_ write and verify. Returns an exit code."""
    try:
        info, data, draft = read_all(portal, item_id)
    except PortalError as exc:
        say("error: could not read the app: %s" % exc)
        return EXIT_READ_FAILED
    decision = decide(info, data, draft, edits, allow_divergent)
    for note in decision.notes:
        say("  note: %s" % note)
    if decision.problems:
        for problem in decision.problems:
            say("  REFUSED: %s" % problem)
        say("VERDICT: REFUSE")
        say("Nothing was written.")
        return EXIT_REFUSED
    todo = [s for s in WRITE_ORDER if decision.states[s] == "apply"]
    for surface in WRITE_ORDER:
        say("  %s: %s" % (surface, "will change %d edit(s)" % len(edits)
                          if surface in todo else "already holds the edits"))
    if not todo:
        say("VERDICT: NOTHING TO DO. Both surfaces already hold the edits.")
        return EXIT_OK
    if not apply_:
        say("VERDICT: ALLOW")
        say("Check only. Nothing was written. Re-run with --apply and "
            "--backup-dir to write: %s." % ", then ".join(todo))
        return EXIT_OK

    say("=== APPLY ===")
    try:
        _info, data_now, draft_now = read_all(portal, item_id)
    except PortalError as exc:
        say("error: could not re-read the app: %s" % exc)
        return EXIT_READ_FAILED
    if not (same(data_now, data) and same(draft_now, draft)):
        say("  REFUSED: a surface changed since it was first read (somebody "
            "saved in the builder?). Nothing was written.")
        return EXIT_REFUSED
    try:
        clash = write_backup(backup_dir, data, draft, edits)
    except OSError as exc:
        say("  REFUSED: the backup could not be written (%s). Nothing was "
            "written to the app." % exc)
        return EXIT_REFUSED
    if clash:
        say("  REFUSED: %s already in the backup folder; pick an empty one. "
            "Nothing was written." % ", ".join(clash))
        return EXIT_REFUSED
    say("  backup kept in %s (data.json, draft.json, inverse.json)"
        % backup_dir)
    originals = {"data": data, "draft": draft}
    done = []
    for surface in todo:
        try:
            write_surface(portal, item_id, info, surface,
                          decision.docs[surface])
        except PortalError as exc:
            say("  FAILED writing %s: %s" % (surface, exc))
            roll_back(portal, item_id, info, done, originals, say)
            return EXIT_WRITE_FAILED
        done.append(surface)
        say("  wrote %s" % surface)

    problems, notes = [], []
    for attempt in range(1, retries + 1):
        problems, notes = [], []
        try:
            for surface in SURFACES:
                found, extra = verify_surface(
                    surface, decision.docs[surface],
                    read_surface(portal, item_id, surface), edits)
                problems.extend(found)
                notes.extend(extra)
        except PortalError as exc:
            problems = ["could not read back: %s" % exc]
        if not problems:
            break
        if attempt < retries:
            time.sleep(delay)
    for note in notes:
        say("  note: %s" % note)
    if problems:
        for problem in problems:
            say("  UNVERIFIED: %s" % problem)
        say("VERDICT: WRITTEN BUT NOT VERIFIED")
        say("To undo, run this command again with --edits %s --apply."
            % os.path.join(backup_dir, "inverse.json"))
        return EXIT_UNVERIFIED
    say("VERDICT: WRITTEN AND VERIFIED on both surfaces (read %d time(s))"
        % attempt)
    return EXIT_OK


# ------------------------------------------------------------------ self-test

def _make_stub(stub):
    """Build the request handler for a stub portal. The stub holds the state."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            raw = body if isinstance(body, bytes) else \
                json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def _error(self, code, message):
            self._send(200, {"error": {"code": code, "message": message}})

        def _handle(self, method, fields):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            stub.reqs.append({"method": method, "path": path,
                              "has_ts": "_ts" in fields,
                              "cache": self.headers.get("Cache-Control"),
                              "referer": self.headers.get("Referer"),
                              "text": fields.get("text")})
            for prefix in ("/redir", "/html", "/err", "/echo"):
                if path.startswith(prefix + "/"):
                    path = path[len(prefix):]
                    if prefix == "/redir":
                        return self._send(302, b"", extra={"Location": path})
                    if prefix == "/html":
                        return self._send(200, b"<html>", "text/html")
                    if prefix == "/echo":
                        return self._error(400, "bad token %s"
                                           % fields.get("token"))
                    return self._error(403, "Not permitted")
            if stub.token and fields.get("token") != stub.token:
                return self._error(498, "Invalid token")
            parts = path[len("/sharing/rest/"):].split("/")
            if method == "GET" and parts[:2] == ["content", "items"]:
                return self._get(parts[2:])
            if method == "POST" and parts[:2] == ["content", "users"]:
                return self._post(parts[2:], fields)
            return self._error(404, "Not found")

        def _get(self, rest):
            if rest[0] != stub.item_id:
                return self._error(404, "Item does not exist")
            if len(rest) == 1:
                info = dict(stub.info)
                if stub.hide_folder:
                    del info["ownerFolder"]
                return self._send(200, info)
            if rest[1:] == ["data"]:
                return self._send(200, stub.data)
            if rest[1:] == ["resources", "config", "config.json"]:
                served = stub.draft
                if stub.stale_left > 0:
                    stub.stale_left -= 1
                    served = stub.draft_prev
                stub.draft_reads += 1
                self._send(200, served)
                if stub.after_draft_read is not None and \
                        stub.draft_reads == stub.after_draft_read[0]:
                    stub.after_draft_read[1](stub)
                return None
            return self._error(404, "Resource does not exist")

        def _post(self, rest, fields):
            owner, rest, folder = rest[0], rest[1:], None
            if rest[0] != "items":
                folder, rest = rest[0], rest[1:]
            item, action = rest[1], rest[2]
            if owner != stub.info["owner"] or item != stub.item_id:
                return self._error(404, "Item does not exist")
            if action == "update":
                if folder != stub.info.get("ownerFolder"):
                    return self._error(404, "Item does not exist in folder")
                mode = stub.next_mode(stub.update_modes)
                if mode == "http500":
                    return self._send(500, b"boom", "text/plain")
                if mode == "fail":
                    return self._send(200, {"success": False})
                if mode == "ok":
                    stub.data = json.loads(fields["text"])
                return self._send(200, {"success": True, "id": item})
            if folder or fields.get("fileName") != "config.json" or \
                    fields.get("resourcesPrefix") != "config":
                return self._error(404, "Resource does not exist")
            mode = stub.next_mode(stub.resource_modes)
            if mode == "http500":
                return self._send(500, b"boom", "text/plain")
            if mode == "ok":
                stub.draft_prev = stub.draft
                stub.stale_left = stub.stale_reads
                stub.draft = json.loads(fields["text"])
                if stub.normalise:
                    stub.draft["widgets"]["widget_2"]["config"]["note"] += " "
            return self._send(200, {"success": True, "itemId": item,
                                    "owner": owner, "folder": folder})

        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._handle("GET", dict((k, v[0]) for k, v in query.items()))

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            body = urllib.parse.parse_qs(self.rfile.read(size).decode("ascii"))
            self._handle("POST", dict((k, v[0]) for k, v in body.items()))

    return Handler


class _Stub(object):
    """A local stand-in for the portal REST calls. It is test scaffolding only."""

    def __init__(self, data, draft, token="tok-3f9a-synthetic", folder=None):
        self.item_id = "0123456789abcdef0123456789abcdef"
        self.token = token
        self.info = {"id": self.item_id, "type": ITEM_TYPE,
                     "owner": "synthuser", "ownerFolder": folder}
        self.data = copy.deepcopy(data)
        self.draft = copy.deepcopy(draft)
        self.draft_prev = None
        self.stale_reads = 0
        self.stale_left = 0
        self.draft_reads = 0
        self.after_draft_read = None
        self.hide_folder = False
        self.normalise = False
        self.update_modes = []
        self.resource_modes = []
        self.reqs = []
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _make_stub(self))
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    @staticmethod
    def next_mode(modes):
        return modes.pop(0) if modes else "ok"

    def posts(self):
        return [r["path"].rsplit("/", 1)[1] for r in self.reqs
                if r["method"] == "POST"]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()


def _fixture():
    """Synthetic data and draft. The two differ only in rich-text markup."""
    data = {"widgets": {
                "widget_1": {"id": "widget_1",
                             "config": {"title": "Trail Map", "zoom": 8,
                                        "tools": ["pan", "zoom"]}},
                "widget_2": {"id": "widget_2",
                             "config": {"note": "<ul data-list=\"bullet\">"
                                                "<li>open</li></ul>",
                                        "limit": 5}}},
            "dataSources": {"dataSource_1": {"type": "WEB_MAP"}}}
    draft = copy.deepcopy(data)
    draft["widgets"]["widget_2"]["config"]["note"] = "<ul><li>open</li></ul>"
    return data, draft


TITLE_EDIT = [{"path": "/widgets/widget_1/config/title",
               "expect": "Trail Map", "value": "Park Map"}]


def _slurp(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


class _Harness(object):
    """The check()/raises() counters. A class so the self-test can test them."""

    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, cond, label):
        if cond:
            self.passed += 1
            print("PASS  %s" % label)
        else:
            self.failed.append(label)
            print("FAIL  %s" % label)

    def raises(self, fn, label, kind=ValueError):
        try:
            fn()
        except kind:
            self.check(True, label)
        except Exception as exc:
            self.check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            self.check(False, "%s (no error raised)" % label)

    def report(self):
        print("-" * 68)
        total = self.passed + len(self.failed)
        if self.failed:
            print("%d assertions, %d failed" % (total, len(self.failed)))
            for label in self.failed:
                print("  FAILED: %s" % label)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0


def self_test():
    """Assertions over the decision core, then every network path on a local stub."""
    harness = _Harness()
    check = harness.check
    raises = harness.raises

    def edits_of(raw):
        return parse_edits(json.dumps(raw))

    def run(stub, tmp, edits=None, name="b", retries=3,
            apply_=True, allow_divergent=False, token=None, root=None):
        lines = []
        portal = Portal(root or stub.url,
                        stub.token if token is None else token, timeout=5)
        code = execute(portal, stub.item_id, edits_of(edits or TITLE_EDIT),
                       apply_, os.path.join(tmp, name), allow_divergent,
                       retries, 0.0, lines.append)
        return code, "\n".join(lines)

    print("exbtwin self-test: temporary files and a local stub only, "
          "no real portal, no credentials")
    print("-" * 68)

    # ---- pointers and paths
    check(parse_pointer("/a/b") == ("a", "b"), "a pointer splits on slashes")
    check(parse_pointer("/a~1b/c~0d") == ("a/b", "c~d"),
          "a pointer unescapes ~1 and ~0")
    check(format_pointer(("a/b", "c~d")) == "/a~1b/c~0d",
          "format_pointer escapes what parse_pointer unescapes")
    raises(lambda: parse_pointer("a/b"), "a pointer must start with a slash")
    raises(lambda: parse_pointer(5), "a pointer must be a string")
    raises(lambda: parse_pointer("/a//b"), "an empty pointer segment is refused")
    raises(lambda: parse_pointer("/"), "the bare root pointer is refused")
    doc = {"a": {"b": [10, {"c": 1}]}, "k": 1}
    check(get_path(doc, ("a", "b", "1", "c")) == (True, 1),
          "get_path walks dicts and list indexes")
    check(get_path(doc, ("a", "x")) == (False, None), "a missing key is not found")
    check(get_path(doc, ("a", "b", "2")) == (False, None),
          "an index past the end is not found")
    check(get_path(doc, ("a", "b", "01")) == (False, None),
          "an index with a leading zero is not found")
    check(get_path(doc, ("a", "b", "x")) == (False, None),
          "a non-numeric list index is not found")
    check(get_path(doc, ("k", "z")) == (False, None),
          "descending into a number is not found")
    check(get_path(doc, ()) == (True, doc), "the empty path is the whole document")
    out = set_path(doc, ("a", "b", "1", "c"), 2)
    check(out["a"]["b"][1]["c"] == 2 and doc["a"]["b"][1]["c"] == 1,
          "set_path replaces in a copy and leaves the original alone")
    check(set_path(doc, ("a", "b", "0"), 11)["a"]["b"][0] == 11,
          "set_path replaces a list element")
    check(same({"a": 1, "b": 2}, {"b": 2, "a": 1}), "same ignores key order")
    check(not same(1, 1.0) and not same(1, True),
          "same tells 1, 1.0 and true apart")
    check(_short("x" * 100).endswith("..."), "long values are shortened in messages")

    # ---- edits file
    good = parse_edits(json.dumps(TITLE_EDIT))
    check(good[0]["value"] == {"data": "Park Map", "draft": "Park Map"},
          "one value applies to both surfaces")
    split = edits_of([{"path": "/a", "expect_data": 1, "expect_draft": 2,
                       "value_data": 3, "value_draft": 4}])
    check(split[0]["expect"] == {"data": 1, "draft": 2}
          and split[0]["value"] == {"data": 3, "draft": 4},
          "per-surface keys override per surface")
    mixed = edits_of([{"path": "/a", "expect": 1, "value": 2,
                       "value_draft": 9}])
    check(mixed[0]["value"] == {"data": 2, "draft": 9},
          "a per-surface key beats the shared key for that surface only")
    raises(lambda: parse_edits("{"), "an edits file that is not JSON raises")
    raises(lambda: parse_edits("{}"), "an edits file that is not a list raises")
    raises(lambda: parse_edits("[]"), "an empty edits list raises")
    raises(lambda: parse_edits("[1]"), "an edit that is not an object raises")
    raises(lambda: edits_of([{"path": "/a", "expect": 1, "value": 2,
                              "valu": 3}]),
           "a misspelt key raises instead of being ignored")
    raises(lambda: edits_of([{"expect": 1, "value": 2}]), "an edit with no path raises")
    raises(lambda: edits_of([{"path": "a", "expect": 1, "value": 2}]),
           "an edit with a bad pointer raises")
    raises(lambda: edits_of([{"path": "/a", "value": 2}]),
           "an edit with no expect raises, so every edit is a compare and swap")
    raises(lambda: edits_of([{"path": "/a", "expect": 1}]), "an edit with no value raises")
    raises(lambda: edits_of([{"path": "/a", "expect": 1, "value": 2,
                              "value_draft": 3, "expect_data": 1},
                             {"path": "/a", "expect": 1, "value": 2}]),
           "two edits on the same path raise")
    raises(lambda: edits_of([{"path": "/a", "expect": 1, "value": 2},
                             {"path": "/a/b", "expect": 1, "value": 2}]),
           "an edit inside another edit raises")
    check(len(edits_of([{"path": "/a/b", "expect": 1, "value": 2},
                        {"path": "/a/c", "expect": 1, "value": 2}])) == 2,
          "sibling paths do not overlap")
    back = parse_edits(json.dumps(inverse_edits(split)))
    check(back[0]["expect"] == {"data": 3, "draft": 4}
          and back[0]["value"] == {"data": 1, "draft": 2},
          "inverse_edits swaps expect and value per surface and re-parses")

    # ---- planning one surface
    state, problems, new = plan_surface({"t": "old"}, edits_of(
        [{"path": "/t", "expect": "old", "value": "new"}]), "data")
    check((state, problems, new) == ("apply", [], {"t": "new"}),
          "a matching expect plans a change")
    state, problems, new = plan_surface({"t": "new"}, edits_of(
        [{"path": "/t", "expect": "old", "value": "new"}]), "data")
    check(state == "applied" and not problems and new == {"t": "new"},
          "a surface that already holds the value is applied, not an error")
    state, problems, new = plan_surface({"t": "other"}, edits_of(
        [{"path": "/t", "expect": "old", "value": "new"}]), "draft")
    check(state == "apply" and len(problems) == 1 and "holds" in problems[0]
          and new == {"t": "other"},
          "a surface holding a third value is a problem and is not changed")
    state, problems, new = plan_surface({}, edits_of(
        [{"path": "/t", "expect": "old", "value": "new"}]), "data")
    check(len(problems) == 1 and "absent" in problems[0],
          "a path that does not exist is a problem: edits replace, never add")

    # ---- structural comparison
    hard, text = diff_docs({"a": 1, "b": [1, 2], "c": "x"},
                           {"a": 1, "b": [1, 2], "c": "y"}, set())
    check(hard == [] and text == [("c",)], "a string-only difference is text, not hard")
    hard, text = diff_docs({"a": 1}, {"a": 1, "b": 2}, set())
    check(hard == [("b",)], "a key on one side only is a hard difference")
    hard, text = diff_docs({"b": [1, 2]}, {"b": [1]}, set())
    check(hard == [("b",)], "a list length difference is hard")
    hard, text = diff_docs({"b": [1, 2]}, {"b": [1, 3]}, set())
    check(hard == [("b", "1")], "a differing list element is hard and is addressed by index")
    hard, text = diff_docs({"a": 1}, {"a": "1"}, set())
    check(hard == [("a",)], "a number against a string is hard")
    hard, text = diff_docs({"a": {"b": 1}}, {"a": [1]}, set())
    check(hard == [("a",)], "a dict against a list is hard")
    hard, text = diff_docs({"a": 1, "b": "x"}, {"a": 2, "b": "y"},
                           set([("a",), ("b",)]))
    check(hard == [] and text == [], "skipped paths are not compared")

    # ---- the decision
    data, draft = _fixture()
    info = {"type": ITEM_TYPE, "owner": "synthuser"}
    ok = decide(info, data, draft, edits_of(TITLE_EDIT))
    check(not ok.problems and ok.states == {"data": "apply", "draft": "apply"}
          and ok.docs["data"]["widgets"]["widget_1"]["config"]["title"]
          == "Park Map", "a clean pair of surfaces is allowed")
    check(len(ok.notes) == 1 and "1 text value" in ok.notes[0],
          "markup that differs between the surfaces is a note, not a refusal")
    check(decide({"type": "Web Map", "owner": "u"}, data, draft,
                 edits_of(TITLE_EDIT)).problems != [],
          "an item that is not a Web Experience is refused")
    check(decide({"type": ITEM_TYPE}, data, draft,
                 edits_of(TITLE_EDIT)).problems != [],
          "an item with no owner is refused")
    drifted = copy.deepcopy(draft)
    drifted["widgets"]["widget_3"] = {"id": "widget_3"}
    refused = decide(info, data, drifted, edits_of(TITLE_EDIT))
    check(len(refused.problems) == 1 and "/widgets/widget_3" in refused.problems[0],
          "a draft with a widget the published copy lacks is refused")
    allowed = decide(info, data, drifted, edits_of(TITLE_EDIT), True)
    check(not allowed.problems and any("widget_3" in n for n in allowed.notes),
          "--allow-divergent turns that refusal into a note")
    both = decide(info, data, draft, edits_of(
        [{"path": "/widgets/widget_1/config/title", "expect": "x",
          "value": "Trail Map"}]))
    check(both.states == {"data": "applied", "draft": "applied"},
          "edits already on both surfaces plan as applied on both")
    check(len(_paths([("a",)] * 7).split(" and ")) == 2
          and _paths([("a",)] * 7).endswith("and 2 more"),
          "a long path list is cut at the limit")
    check(_paths([()]) == "(root)", "an empty path prints as the root")

    # ---- verification
    exp = {"t": "new", "r": "x"}
    p, n = verify_surface("data", exp, {"t": "new", "r": "x"}, edits_of(
        [{"path": "/t", "expect": "old", "value": "new"}]))
    check(p == [] and n == [], "an exact read-back verifies")
    p, n = verify_surface("data", exp, {"t": "old", "r": "x"}, edits_of(
        [{"path": "/t", "expect": "old", "value": "new"}]))
    check(len(p) == 1 and "reads back" in p[0],
          "an edited value that reads back old fails verification")
    p, n = verify_surface("data", exp, {"r": "x"}, edits_of(
        [{"path": "/t", "expect": "old", "value": "new"}]))
    check(len(p) >= 1 and "(absent)" in p[0],
          "an edited path that vanished fails verification")
    p, n = verify_surface("data", exp, {"t": "new", "r": "x", "z": 1}, edits_of(
        [{"path": "/t", "expect": "old", "value": "new"}]))
    check(len(p) == 1 and "outside the edits" in p[0],
          "a key added outside the edits fails verification")
    p, n = verify_surface("data", exp, {"t": "new", "r": "y"}, edits_of(
        [{"path": "/t", "expect": "old", "value": "new"}]))
    check(p == [] and len(n) == 1,
          "text rewritten outside the edits is a note, not a failure")

    # ---- inputs
    ident = "0123456789abcdef0123456789abcdef"
    check(check_item_id(ident) and not check_item_id(ident[:-1])
          and not check_item_id(ident + "\n"), "an item id is exactly 32 hex characters")
    check(check_portal("https://org.example.com") is None
          and check_portal("https://host.example.com/portal") is None,
          "https portals pass")
    check(check_portal("http://127.0.0.1:9") is None
          and check_portal("http://localhost") is None,
          "plain http passes for localhost only")
    for bad in ("http://portal.example.com", "ftp://x.example.com",
                "https://", "https://u:p@x.example.com", "https://x.example.com/?a=1",
                "https://[x"):
        check(check_portal(bad) is not None, "portal %s is refused" % bad)
    tmp = tempfile.mkdtemp(prefix="exbtwin-")
    try:
        tf = os.path.join(tmp, "tok.txt")
        with io.open(tf, "w", encoding="utf-8") as handle:
            handle.write(u"  abc123 \n")
        check(load_token(tf, {}) == "abc123", "a token file is stripped")
        check(load_token(None, {"ARCGIS_TOKEN": " envtok "}) == "envtok",
              "ARCGIS_TOKEN is the fallback")
        check(load_token(None, {}) == "", "no token is an empty string")
        with io.open(tf, "w", encoding="utf-8") as handle:
            handle.write(u"\n")
        raises(lambda: load_token(tf, {}), "an empty token file raises")

        # ---- the parser
        def parse_quiet(argv):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                try:
                    _parse(argv)
                except SystemExit as exc:
                    return exc.code
            return None

        check(parse_quiet(["--ap"]) == EXIT_USAGE,
              "a unique prefix --ap of --apply is REFUSED by the parser  "
              "<-- pinned defect")
        check(parse_quiet(["--backup"]) == EXIT_USAGE,
              "a prefix of --backup-dir is refused by the parser")
        check(parse_quiet(["--apply"]) is None, "the full --apply is accepted")
        base = ["--portal", "https://o.example.com", "--item", ident,
                "--edits", "e.json"]
        args = _parse(base)
        check(usage_problem(args, "") is None, "complete read-only arguments pass")
        check(usage_problem(_parse([]), "") is not None, "no arguments is a usage error")
        check(usage_problem(_parse(base[:2] + ["--item", "zz", "--edits", "e"]),
                            "") is not None, "a bad item id is a usage error")
        check(usage_problem(_parse(["--portal", "http://x.example.com", "--item",
                                    ident, "--edits", "e"]), "") is not None,
              "an http portal is a usage error")
        check(usage_problem(_parse(base + ["--retries", "0"]), "") is not None
              and usage_problem(_parse(base + ["--timeout", "0"]), "") is not None
              and usage_problem(_parse(base + ["--retry-delay", "-1"]), "")
              is not None, "out of range numbers are usage errors")
        check("backup-dir" in usage_problem(_parse(base + ["--apply"]), "t"),
              "--apply without --backup-dir is a usage error")
        check("token" in usage_problem(
            _parse(base + ["--apply", "--backup-dir", "b"]), ""),
              "--apply without a token is a usage error")
        check(usage_problem(_parse(base + ["--apply", "--backup-dir", "b"]),
                            "t") is None, "--apply with a backup folder and a token passes")

        # ---- the network paths, against a local stub
        data, draft = _fixture()
        stub = _Stub(data, draft)
        captured = []
        try:
            code, text = run(stub, tmp, apply_=False)
            check(code == 0 and "VERDICT: ALLOW" in text and "Check only" in text,
                  "a check run on a clean app is allowed")
            check(stub.posts() == [] and not os.path.exists(os.path.join(tmp, "b")),
                  "a check run sends no write and creates no backup folder")
            gets = [r for r in stub.reqs if r["method"] == "GET"]
            check(len(gets) == 3 and all(r["has_ts"] for r in gets)
                  and all(r["cache"] == "no-cache" for r in gets),
                  "every read carries a cache-busting parameter and no-cache")
            check(all(r["referer"] == stub.url for r in gets),
                  "every read sends the portal URL as the Referer")
            captured.append(text)
            del stub.reqs[:]

            code, text = run(stub, tmp)
            captured.append(text)
            check(code == 0 and "WRITTEN AND VERIFIED" in text,
                  "an applied edit is written and verified")
            check(stub.posts() == ["updateResources", "update"],
                  "the draft is written BEFORE the published copy  "
                  "<-- pinned defect")
            check(stub.data["widgets"]["widget_1"]["config"]["title"] == "Park Map"
                  and stub.draft["widgets"]["widget_1"]["config"]["title"]
                  == "Park Map", "both surfaces hold the new title")
            wrote = [r for r in stub.reqs if r["method"] == "POST"]
            check(all(r["referer"] == stub.url for r in wrote),
                  "writes send the portal URL as the Referer")
            check(not any("token" in r["path"] for r in stub.reqs),
                  "the token is never part of a URL path")
            expected_data = copy.deepcopy(data)
            expected_data["widgets"]["widget_1"]["config"]["title"] = "Park Map"
            check(same(stub.data, expected_data),
                  "nothing outside the edit changed on the published copy")
            with io.open(os.path.join(tmp, "b", "data.json"), encoding="utf-8") as h:
                kept = json.load(h)
            with io.open(os.path.join(tmp, "b", "inverse.json"), encoding="utf-8") as h:
                undo = h.read()
            captured.append(undo)
            check(same(kept, data), "the backup holds the old published copy")

            code, text = run(stub, tmp, name="b", edits=[
                {"path": "/widgets/widget_1/config/zoom", "expect": 8,
                 "value": 9}])
            check(code == 1 and "already in the backup folder" in text
                  and "Nothing was written" in text,
                  "an existing backup is never overwritten")
            del stub.reqs[:]
            code, text = run(stub, tmp, name="b2")
            check(code == 0 and "NOTHING TO DO" in text and stub.posts() == [],
                  "a rerun finds both surfaces done and writes nothing")
            check(not os.path.exists(os.path.join(tmp, "b2")),
                  "a rerun that has nothing to do keeps no backup")

            code, text = run(stub, tmp, name="b3", edits=json.loads(undo))
            check(code == 0 and same(stub.data, data)
                  and same(stub.draft, draft),
                  "the inverse edits written by a run restore both surfaces")
        finally:
            stub.close()

        # a half finished earlier run: the draft holds the edit, the data not
        data, draft = _fixture()
        half = copy.deepcopy(draft)
        half["widgets"]["widget_1"]["config"]["title"] = "Park Map"
        stub = _Stub(data, half)
        try:
            code, text = run(stub, tmp, name="half")
            check(code == 0 and stub.posts() == ["update"]
                  and "draft: already holds" in text,
                  "a rerun after a half finished run writes only the missing surface")
        finally:
            stub.close()

        # a folder item takes the folder in the update path only
        stub = _Stub(data, draft, folder="f0lder1d")
        try:
            code, text = run(stub, tmp, name="fold")
            paths = [r["path"] for r in stub.reqs if r["method"] == "POST"]
            check(code == 0 and paths[0].endswith(
                "/users/synthuser/items/%s/updateResources" % stub.item_id)
                  and "/synthuser/f0lder1d/items/" in paths[1],
                  "the folder id is in the update path and not the updateResources path")
        finally:
            stub.close()

        # rich text: one expect cannot match both surfaces
        stub = _Stub(data, draft)
        note_edit = [{"path": "/widgets/widget_2/config/note",
                      "expect": "<ul><li>open</li></ul>",
                      "value": "<ul><li>closed</li></ul>"}]
        try:
            code, text = run(stub, tmp, edits=note_edit, name="rt")
            check(code == 1 and stub.posts() == [] and "data: " in text,
                  "a replacement written against one surface is REFUSED on the "
                  "other, not silently skipped  <-- pinned defect")
            split_edit = [{"path": "/widgets/widget_2/config/note",
                           "expect_data": data["widgets"]["widget_2"]["config"]["note"],
                           "expect_draft": "<ul><li>open</li></ul>",
                           "value_data": "<ul data-list=\"bullet\"><li>closed</li></ul>",
                           "value_draft": "<ul><li>closed</li></ul>"}]
            code, text = run(stub, tmp, edits=split_edit, name="rt2")
            check(code == 0 and "WRITTEN AND VERIFIED" in text,
                  "per-surface expect and value write rich text to both surfaces")
        finally:
            stub.close()

        # divergence between the surfaces
        data, draft = _fixture()
        extra = copy.deepcopy(draft)
        extra["widgets"]["widget_3"] = {"id": "widget_3"}
        stub = _Stub(data, extra)
        try:
            code, text = run(stub, tmp, name="dv")
            check(code == 1 and stub.posts() == [] and "unpublished" in text,
                  "unpublished builder work refuses the write")
            code, text = run(stub, tmp, name="dv2", allow_divergent=True)
            check(code == 0 and "widget_3" in text
                  and "widget_3" in stub.draft["widgets"],
                  "--allow-divergent writes and keeps the unpublished work")
        finally:
            stub.close()

        # a wrong item type
        stub = _Stub(data, draft)
        stub.info["type"] = "Web Map"
        try:
            code, text = run(stub, tmp, name="ty")
            check(code == 1 and "not Web Experience" in text and stub.posts() == [],
                  "an item that is not a Web Experience is refused before any write")
        finally:
            stub.close()

        # the silent no-op: the portal says success and stores nothing
        stub = _Stub(data, draft)
        stub.update_modes = ["noop"]
        try:
            code, text = run(stub, tmp, name="noop", retries=2)
            check(code == 3 and "WRITTEN BUT NOT VERIFIED" in text
                  and "UNVERIFIED: data:" in text
                  and "inverse.json" in text,
                  "a write the portal acknowledges but did not store is caught  "
                  "<-- pinned defect")
        finally:
            stub.close()

        # CDN staleness
        stub = _Stub(data, draft)
        stub.stale_reads = 1
        try:
            code, text = run(stub, tmp, name="st")
            check(code == 0 and "read 2 time(s)" in text,
                  "one stale read of the draft is retried and then verifies")
        finally:
            stub.close()
        stub = _Stub(data, draft)
        stub.stale_reads = 50
        try:
            code, text = run(stub, tmp, name="st2", retries=2)
            check(code == 3 and "UNVERIFIED: draft:" in text,
                  "a draft that stays stale fails verification instead of passing")
        finally:
            stub.close()

        # somebody saved in the builder between the check and the write
        stub = _Stub(data, draft)

        def builder_save(s):
            s.draft["widgets"]["widget_1"]["config"]["zoom"] = 9

        stub.after_draft_read = (1, builder_save)
        try:
            code, text = run(stub, tmp, name="race")
            check(code == 1 and stub.posts() == [] and "changed since" in text,
                  "a surface that changed between the check and the write "
                  "refuses the write")
        finally:
            stub.close()

        # write failures and rollback
        data, draft = _fixture()
        stub = _Stub(data, draft)
        stub.update_modes = ["http500"]
        try:
            code, text = run(stub, tmp, name="f1")
            check(code == 2 and "FAILED writing data" in text
                  and "read it back: restored" in text and same(stub.draft, draft),
                  "a failed published write restores the draft and reads it back")
        finally:
            stub.close()
        stub = _Stub(data, draft)
        stub.update_modes = ["fail"]
        stub.resource_modes = ["ok", "noop"]
        try:
            code, text = run(stub, tmp, name="f2")
            check(code == 2 and "does not match" in text
                  and "did not report success" in text,
                  "a rollback the portal ignores is reported, not assumed")
        finally:
            stub.close()
        stub = _Stub(data, draft)
        stub.update_modes = ["http500"]
        stub.resource_modes = ["ok", "http500"]
        try:
            code, text = run(stub, tmp, name="f3")
            check(code == 2 and "rollback of draft FAILED" in text,
                  "a rollback that errors says to restore from the backup")
        finally:
            stub.close()
        stub = _Stub(data, draft)
        stub.resource_modes = ["http500"]
        try:
            code, text = run(stub, tmp, name="f4")
            check(code == 2 and "FAILED writing draft" in text
                  and stub.posts() == ["updateResources"]
                  and "rolled back" not in text,
                  "a failed draft write stops before touching the published copy")
        finally:
            stub.close()

        # reads that fail
        stub = _Stub(data, draft)
        try:
            code, text = run(stub, tmp, name="r1", token="not-the-token")
            check(code == 4 and "Invalid token" in text
                  and "not-the-token" not in text,
                  "a wrong token is a read failure that does not echo the token")
            captured.append(text)
            code, text = run(stub, tmp, name="r2", root=stub.url + "/html")
            check(code == 4 and "did not return JSON" in text,
                  "a reply that is not JSON is a read failure")
            code, text = run(stub, tmp, name="r3", root=stub.url + "/err")
            check(code == 4 and "Not permitted" in text,
                  "a portal error body is a read failure")
            code, text = run(stub, tmp, name="r3b", root=stub.url + "/echo")
            captured.append(text)
            check(code == 4 and "bad token ***" in text
                  and stub.token not in text,
                  "a portal error that echoes the token is redacted before it "
                  "is printed  <-- pinned defect")
            before = len(stub.reqs)
            code, text = run(stub, tmp, name="r4", root=stub.url + "/redir")
            check(code == 4 and len(stub.reqs) == before + 1,
                  "a redirect is never followed, so the token stays on the "
                  "named host")
            ghost = Portal(stub.url, stub.token, timeout=5)
            lines = []
            code = execute(ghost, "f" * 32, edits_of(TITLE_EDIT), False,
                           os.path.join(tmp, "r5"), False, 1, 0.0, lines.append)
            check(code == 4 and "does not exist" in lines[0],
                  "an item id the portal does not know is a read failure")
            ghost.call = lambda path, fields=None: ["not", "an", "object"]
            raises(lambda: read_all(ghost, ident), "an item record that is not "
                   "an object raises", PortalError)
            ghost.call = lambda path, fields=None: (
                {"type": ITEM_TYPE} if "resources" not in path
                and "data" not in path else [1])
            raises(lambda: read_all(ghost, ident), "a surface that is not an "
                   "object raises", PortalError)
        finally:
            stub.close()
        port_url = stub.url
        code, text = run(stub, tmp, name="r6", root=port_url)
        check(code == 4 and "could not read" in text,
              "an unreachable portal is a read failure")

        # the stub is strict, so a wrong URL or form would fail the tool
        data, draft = _fixture()
        stub = _Stub(data, draft)
        try:
            strict = Portal(stub.url, stub.token, 5)
            raises(lambda: strict.call("content/nowhere"),
                   "the stub refuses an unknown route", PortalError)
            raises(lambda: strict.call(
                "content/items/%s/resources/other.json" % stub.item_id),
                   "the stub refuses a resource other than config/config.json",
                   PortalError)
            raises(lambda: strict.call(
                "content/users/someone/items/%s/update" % stub.item_id,
                {"text": "{}"}), "the stub refuses a write under another owner",
                   PortalError)
            raises(lambda: strict.call(
                "content/users/synthuser/items/%s/updateResources"
                % stub.item_id, {"fileName": "other.json",
                                 "resourcesPrefix": "config", "text": "{}"}),
                   "the stub refuses a resource write with the wrong file name",
                   PortalError)
            check(same(stub.data, data) and same(stub.draft, draft),
                  "none of those refused calls changed the stub")
            code, text = run(stub, tmp, name="nrm", edits=TITLE_EDIT)
            check(code == 0, "a clean run on the strict stub")
        finally:
            stub.close()

        # the portal rewrites markup outside the edit while saving the draft
        stub = _Stub(data, draft)
        stub.normalise = True
        try:
            code, text = run(stub, tmp, name="nrm2")
            check(code == 0 and "rewritten by the portal" in text,
                  "text the portal rewrites outside the edit is a note, not a "
                  "failure")
        finally:
            stub.close()

        # a token that cannot see ownerFolder writes to the wrong update path
        stub = _Stub(data, draft, folder="f0lder1d")
        stub.hide_folder = True
        try:
            code, text = run(stub, tmp, name="hf")
            check(code == 2 and "FAILED writing data" in text
                  and "restored" in text and same(stub.draft, draft),
                  "an update path the portal rejects fails the write and "
                  "restores the draft")
        finally:
            stub.close()

        # a public app can be checked with no token at all
        stub = _Stub(data, draft, token=None)
        try:
            code, text = run(stub, tmp, name="pub", apply_=False, token="")
            check(code == 0 and "VERDICT: ALLOW" in text,
                  "a check run needs no token for a public app")
        finally:
            stub.close()

        # backup folder that cannot be created
        data, draft = _fixture()
        stub = _Stub(data, draft)
        blocker = os.path.join(tmp, "blocker")
        with io.open(blocker, "w", encoding="utf-8") as handle:
            handle.write(u"x")
        try:
            lines = []
            code = execute(Portal(stub.url, stub.token, 5), stub.item_id,
                           edits_of(TITLE_EDIT), True,
                           os.path.join(blocker, "sub"), False, 1, 0.0,
                           lines.append)
            check(code == 1 and "backup could not be written" in lines[-1]
                  and stub.posts() == [],
                  "a backup folder that cannot be made refuses before any write")
        finally:
            stub.close()

        # the read-back itself failing
        data, draft = _fixture()
        stub = _Stub(data, draft)
        try:
            portal = Portal(stub.url, stub.token, 5)
            real = portal.call
            state = {"n": 0}

            def flaky(path, fields=None):
                if fields is None and "data" in path and path.endswith("/data"):
                    state["n"] += 1
                    if state["n"] >= 3:
                        raise PortalError("read-back failed")
                return real(path, fields)

            portal.call = flaky
            lines = []
            code = execute(portal, stub.item_id, edits_of(TITLE_EDIT), True,
                           os.path.join(tmp, "flaky"), False, 2, 0.0,
                           lines.append)
            text = "\n".join(lines)
            check(code == 3 and "could not read back" in text,
                  "a read-back that errors is unverified, never assumed good")
            lines = []
            state["n"] = -99
            code = execute(portal, stub.item_id, edits_of(TITLE_EDIT), True,
                           os.path.join(tmp, "flaky2"), False, 2, 0.0,
                           lines.append)
            check(code == 0, "the same app verifies once the reads work")
            state["n"] = 0

            def flaky_second(path, fields=None):
                if fields is None and path.endswith("/data"):
                    state["n"] += 1
                    if state["n"] == 2:
                        raise PortalError("re-read failed")
                return real(path, fields)

            portal.call = flaky_second
            lines = []
            code = execute(portal, stub.item_id, edits_of(
                [{"path": "/widgets/widget_1/config/zoom", "expect": 8,
                  "value": 9}]), True, os.path.join(tmp, "flaky3"), False, 2,
                           0.0, lines.append)
            check(code == 4 and "could not re-read" in "\n".join(lines),
                  "a failed re-read before the write refuses it")
        finally:
            stub.close()

        # ---- the command line, end to end
        data, draft = _fixture()
        stub = _Stub(data, draft)
        try:
            ef = os.path.join(tmp, "edits.json")
            with io.open(ef, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(TITLE_EDIT))
            tf = os.path.join(tmp, "tok.txt")
            with io.open(tf, "w", encoding="utf-8") as handle:
                handle.write(stub.token)
            argv = ["--portal", stub.url, "--item", stub.item_id,
                    "--edits", ef, "--timeout", "5", "--retry-delay", "0"]

            def cli(extra):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    code = main(argv + extra)
                captured.append(out.getvalue() + err.getvalue())
                return code, out.getvalue(), err.getvalue()

            code, out, err = cli(["--token-file", tf])
            check(code == 0 and "VERDICT: ALLOW" in out and stub.posts() == [],
                  "the command line check run works with a token file")
            code, out, err = cli(["--apply", "--token-file", tf])
            check(code == 64 and "--backup-dir" in err,
                  "the command line refuses --apply with no backup folder")
            code, out, err = cli(["--apply", "--backup-dir",
                                  os.path.join(tmp, "cli")])
            check(code == 64 and "token" in err,
                  "the command line refuses --apply with no token")
            code, out, err = cli(["--apply", "--token-file", tf,
                                  "--backup-dir", os.path.join(tmp, "cli")])
            check(code == 0 and "WRITTEN AND VERIFIED" in out
                  and stub.posts() == ["updateResources", "update"],
                  "the command line --apply writes both surfaces and verifies")
            captured.append("\n".join(
                _slurp(os.path.join(tmp, "cli", n)) for n in BACKUP_FILES))
            code, out, err = cli(["--token-file", os.path.join(tmp, "nope.txt")])
            check(code == 64 and "token" in err,
                  "a missing token file is a usage error")
            with io.open(tf, "w", encoding="utf-8") as handle:
                handle.write(u"\n")
            code, out, err = cli(["--token-file", tf])
            check(code == 64 and "empty" in err, "an empty token file is a usage error")
            code, out, err = cli(["--edits", os.path.join(tmp, "nope.json")])
            check(code == 64 and "edits" in err, "a missing edits file is a usage error")
            with io.open(ef, "w", encoding="utf-8") as handle:
                handle.write(u"[]")
            code, out, err = cli([])
            check(code == 64 and "non-empty" in err,
                  "a malformed edits file is a usage error")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main([])
            check(code == 64, "the command line with no arguments is a usage error")
        finally:
            stub.close()

        # ---- no token in any output or file this run produced
        secret = "tok-3f9a-synthetic"
        files = []
        for root_dir, _dirs, names in os.walk(tmp):
            for name in names:
                with io.open(os.path.join(root_dir, name), encoding="utf-8") as h:
                    files.append(h.read())
        check(secret not in "\n".join(captured) and secret not in "\n".join(files),
              "the token is in no output and in no file written")
        check(len(captured) > 5 and len(files) > 5,
              "the token search read real output and real files")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    inner = _Harness()
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        inner.check(False, "x")
        inner.raises(lambda: None, "y")
        inner.raises(lambda: 1 // 0, "z")
        inner.raises(lambda: int("q"), "w")
        inner_code = inner.report()
    check(inner_code == 1 and inner.passed == 1 and len(inner.failed) == 3
          and "4 assertions, 3 failed" in sink.getvalue(),
          "the harness counts a failing check, a missing error and a wrong "
          "error as failures")
    check("main" in runpy.run_path(os.path.abspath(__file__),
                                   run_name="exbtwin_import"),
          "loading the file runs nothing by itself and exposes main")
    return harness.report()


# ----------------------------------------------------------------------- cli

class _Parser(argparse.ArgumentParser):
    """Exit 64 on a bad command line. argparse's own 2 would mean a failed write."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, "%s: error: %s\n" % (self.prog, message))


def _parse(argv):
    ap = _Parser(
        prog="exbtwin.py",
        allow_abbrev=False,
        description="Write an Experience Builder edit to both config "
                    "surfaces, then read both back.",
        epilog="Nothing is written without --apply. The token comes from "
               "--token-file or ARCGIS_TOKEN, never from a flag.")
    ap.add_argument("--portal",
                    help="portal root, e.g. https://example.maps.arcgis.com")
    ap.add_argument("--item", help="the 32 character item id of the app")
    ap.add_argument("--edits", help="JSON file listing the edits")
    ap.add_argument("--token-file", dest="token_file",
                    help="file holding the token and nothing else")
    ap.add_argument("--backup-dir", dest="backup_dir",
                    help="empty folder that keeps both old copies and "
                         "inverse.json. Required with --apply.")
    ap.add_argument("--allow-divergent", dest="allow_divergent",
                    action="store_true",
                    help="write although the draft holds unpublished work")
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                    help="read-backs before giving up (default %d)"
                         % DEFAULT_RETRIES)
    ap.add_argument("--retry-delay", dest="retry_delay", type=float,
                    default=DEFAULT_RETRY_DELAY,
                    help="seconds between read-backs (default %.1f)"
                         % DEFAULT_RETRY_DELAY)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help="seconds per request (default %.0f)"
                         % DEFAULT_TIMEOUT)
    ap.add_argument("--apply", action="store_true",
                    help="perform the write. Without it nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return self_test()
    try:
        token = load_token(args.token_file, os.environ)
    except (OSError, ValueError) as exc:
        print("error: the token file: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    problem = usage_problem(args, token)
    if problem:
        print("error: %s" % problem, file=sys.stderr)
        return EXIT_USAGE
    try:
        with io.open(args.edits, encoding="utf-8-sig") as handle:
            edits = parse_edits(handle.read())
    except (OSError, ValueError) as exc:
        print("error: the edits file: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    portal = Portal(args.portal, token, args.timeout)
    return execute(portal, args.item, edits, args.apply, args.backup_dir,
                   args.allow_divergent, args.retries, args.retry_delay, print)


if __name__ == "__main__":
    sys.exit(main())
