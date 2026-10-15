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
stale draft). It uploads the draft as a multipart FILE, because ArcGIS Online
has been seen to strip style= attributes and image sources from a text=
upload. It then re-reads both surfaces with the CDN cache defeated and exits
non-zero unless every edited value reads back, no markup was lost and nothing
else moved. If a write raises, it reads the surface again before it decides
whether to roll back, because the portal may have stored a write whose reply
was lost.

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
not verified, 4 the app could not be read, 64 usage error, 70 a fault in the
tool itself.
"""

import argparse
import collections
import contextlib
import copy
import email.parser
import http.client
import http.server
import io
import json
import math
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid

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
EXIT_FAULT = 70          # 1 means "refused", so a crash must not exit 1

SURFACES = ("data", "draft")
WRITE_ORDER = ("draft", "data")        # draft first: see the module docstring
EDIT_KEYS = frozenset(["path", "expect", "expect_data", "expect_draft",
                       "value", "value_data", "value_draft"])
BACKUP_FILES = ("data.json", "draft.json", "inverse.json")

_ITEM_ID = re.compile(r"^[0-9a-fA-F]{32}\Z")
_INDEX = re.compile(r"^(0|[1-9][0-9]*)\Z")
# Markup the portal is known to strip from a text= upload: inline styles, and
# image sources that are blanked to src='' or src="".
_STYLE = re.compile(r"\bstyle\s*=")
_EMPTY_SRC = re.compile(r"\bsrc\s*=\s*(?:''|\"\")")

Decision = collections.namedtuple("Decision", "problems notes states docs")


class PortalError(Exception):
    """A request failed or the portal answered with an error."""


# ------------------------------------------------------------ pure: values

def same(a, b):
    """JSON equality that tells 1, 1.0 and true apart and ignores key order."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _normal(value):
    """Turn every whole-number float into an int, so 8.0 and 8 compare equal."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_normal(v) for v in value]
    if isinstance(value, dict):
        return dict((k, _normal(v)) for k, v in value.items())
    return value


def same_loose(a, b):
    """Like same(), but 8 and 8.0 are one value. JavaScript writes 8.0 as 8."""
    return same(_normal(a), _normal(b))


def _no_constant(name):
    raise ValueError("%s is not valid JSON" % name)


def _no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("the key %s appears twice in one object"
                             % json.dumps(key))
        seen[key] = value
    return seen


def loads(text):
    """json.loads that refuses NaN, Infinity and a repeated key.

    Python keeps the last of two equal keys and accepts NaN, and a copy of the
    document written back would then differ from the one that was read.
    """
    return json.loads(text, parse_constant=_no_constant,
                      object_pairs_hook=_no_duplicates)


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
        raw = loads(text)
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
        elif same_loose(current, want):
            applied += 1
        elif not same_loose(current, edit["expect"][surface]):
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
    rich-text markup when it saves the draft, so text alone is not drift. A
    whole-number float equals the int (8.0 and 8): the portal may normalise it.
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
        elif not same_loose(x, y):
            hard.append(path)

    walk(a, b, ())
    return hard, text


def markup_marks(doc, skip):
    """Count (style= attributes, empty src values) in every string outside skip."""
    counts = [0, 0]

    def walk(node, path):
        if path in skip:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, path + (key,))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, path + (str(index),))
        elif isinstance(node, str):
            counts[0] += len(_STYLE.findall(node))
            counts[1] += len(_EMPTY_SRC.findall(node))

    walk(doc, ())
    return tuple(counts)


def lost_markup(before, after):
    """Describe markup that `after` lost against `before`, else an empty string."""
    if after[0] < before[0] or after[1] > before[1]:
        return ("style= attributes %d against %d, empty src values %d "
                "against %d" % (after[0], before[0], after[1], before[1]))
    return ""


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
    findings = []
    if hard:
        findings.append("the draft and the published copy differ in "
                        "structure at %s, which means unpublished builder "
                        "work; the next Publish would ship it" % _paths(hard))
    lost = lost_markup(markup_marks(data, skip), markup_marks(draft, skip))
    if lost:
        findings.append("the draft holds less markup than the published "
                        "copy (%s). A text= upload strips markup, so the "
                        "draft may have been damaged; upload the published "
                        "JSON as the resource file to repair it, and the "
                        "next Publish would otherwise ship the loss" % lost)
    for message in findings:
        if allow_divergent:
            notes.append(message)
        else:
            problems.append(message + ". Review it, then pass "
                            "--allow-divergent to proceed")
    if text:
        notes.append("%d text value(s) differ between the surfaces outside "
                     "the edits (%s); the builder rewrites markup, so this is "
                     "not treated as drift. A changed id, url or label would "
                     "pass here too, so read the list" % (len(text),
                                                          _paths(text)))
    return Decision(problems, notes, states, docs)


def verify_surface(surface, expected, actual, edits):
    """Compare a re-read surface with what was written. Returns (problems, notes)."""
    problems, notes = [], []
    for edit in edits:
        found, current = get_path(actual, edit["parts"])
        if not found or not same_loose(current, edit["value"][surface]):
            problems.append("%s: %s reads back %s, wanted %s"
                            % (surface, edit["path"],
                               _short(current) if found else "(absent)",
                               _short(edit["value"][surface])))
    skip = set(edit["parts"] for edit in edits)
    hard, text = diff_docs(expected, actual, skip)
    if hard:
        problems.append("%s: content changed outside the edits at %s"
                        % (surface, _paths(hard)))
    lost = lost_markup(markup_marks(expected, skip),
                       markup_marks(actual, skip))
    if lost:
        problems.append("%s: the portal stripped markup outside the edits "
                        "(%s)" % (surface, lost))
    if text:
        notes.append("%s: %d text value(s) outside the edits were rewritten "
                     "by the portal, markup intact (%s)"
                     % (surface, len(text), _paths(text)))
    return problems, notes


# ------------------------------------------------------------ pure: input

def check_item_id(value):
    return bool(_ITEM_ID.match(value))


def check_portal(url):
    """Return an error string for an unusable portal URL, else None."""
    if any(ord(c) <= 32 or ord(c) >= 127 for c in url):
        return ("the portal URL must be plain ASCII with no space or "
                "control character")
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        parsed.port             # raises ValueError for https://host:abc
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
    """Return the token from a file, else ARCGIS_TOKEN, else an empty string.

    utf-8-sig, because Windows PowerShell 5 writes a byte order mark that
    str.strip() would leave inside the token.
    """
    if token_file:
        with io.open(token_file, encoding="utf-8-sig") as handle:
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
    if not (args.retries >= 1 and args.retry_delay >= 0 and args.timeout > 0
            and math.isfinite(args.retry_delay)
            and math.isfinite(args.timeout)):
        return ("--retries must be at least 1, --retry-delay cannot be "
                "negative and --timeout must be positive, and both must be "
                "real numbers (not nan or inf).")
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


def _multipart(form, upload):
    """Build a multipart/form-data body. upload is (field, name, bytes, type)."""
    boundary = "exbtwin" + uuid.uuid4().hex
    parts = []
    for key in sorted(form):
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"'
                      '\r\n\r\n%s\r\n' % (boundary, key, form[key]))
                     .encode("utf-8"))
    field, name, payload, ctype = upload
    parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"; '
                  'filename="%s"\r\nContent-Type: %s\r\n\r\n'
                  % (boundary, field, name, ctype)).encode("utf-8"))
    parts.append(payload + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode("ascii"))
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def _is_portal_error(doc):
    """True for the one reply shape ArcGIS uses: {"error": {"code": N, ...}}.

    A config with its own top-level "error" key has other keys, or no
    integer code, and must be read as data.
    """
    if not (isinstance(doc, dict) and list(doc) == ["error"]):
        return False
    error = doc["error"]
    return (isinstance(error, dict) and isinstance(error.get("code"), int)
            and not isinstance(error.get("code"), bool))


class Portal(object):
    """The few ArcGIS REST calls this tool needs. Every call sends f=json."""

    def __init__(self, root, token="", timeout=DEFAULT_TIMEOUT):
        self.root = root.rstrip("/")
        self.base = self.root + "/sharing/rest/"
        self.token = token
        self.timeout = timeout
        self.calls = 0
        self.posts = 0
        handlers = [_NoRedirect]
        if urllib.parse.urlparse(self.root).hostname in LOCAL_HOSTS:
            # A proxy cannot reach this machine's loopback, so never use one.
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)

    def _clean(self, text):
        """Replace the token, raw and percent-encoded, with ***."""
        if not self.token:
            return text
        for form in (self.token, urllib.parse.quote_plus(self.token),
                     urllib.parse.quote(self.token, safe="")):
            text = text.replace(form, "***")
        return text

    def call(self, path, fields=None, upload=None):
        """GET when fields is None, else POST them. Returns the parsed JSON.

        upload, (field, name, bytes, type), turns the POST into multipart.
        """
        self.calls += 1
        form = dict(fields or {})
        form["f"] = "json"
        if self.token:
            form["token"] = self.token
        headers = {"Cache-Control": "no-cache", "Referer": self.root}
        try:
            if fields is None:
                # A throwaway parameter defeats the CDN that fronts item
                # resources.
                form["_ts"] = "%d%d" % (time.time() * 1000, self.calls)
                request = urllib.request.Request(
                    self.base + path + "?" + urllib.parse.urlencode(form),
                    headers=headers)
            else:
                self.posts += 1
                if upload is None:
                    body = urllib.parse.urlencode(form).encode("ascii")
                else:
                    body, headers["Content-Type"] = _multipart(form, upload)
                request = urllib.request.Request(
                    self.base + path, data=body, headers=headers)
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read()
        except (OSError, ValueError, http.client.HTTPException) as exc:
            # HTTPException: a reply cut short (IncompleteRead) or a request
            # line http.client refuses (InvalidURL, which quotes the token).
            raise PortalError(self._clean("%s: %s" % (path, exc)))
        try:
            doc = loads(body.decode("utf-8"))
        except ValueError:
            raise PortalError("%s did not return JSON" % path)
        if _is_portal_error(doc):
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
    """Write one whole surface. Raises PortalError unless the portal says success.

    The item data goes up as text=. The draft goes up as a multipart FILE:
    ArcGIS Online has been seen to strip style= attributes and blank image
    sources from a text= resource upload (Esri documents text= for
    updateResources but says nothing about sanitising it).
    """
    owner = urllib.parse.quote(info["owner"], safe="")
    text = json.dumps(doc, separators=(",", ":"))
    if surface == "data":
        folder = info.get("ownerFolder")
        where = owner + "/" + urllib.parse.quote(str(folder), safe="") \
            if folder else owner
        answer = portal.call("content/users/%s/items/%s/update"
                             % (where, item_id), {"text": text})
    else:
        answer = portal.call(
            "content/users/%s/items/%s/updateResources" % (owner, item_id),
            {"fileName": DRAFT_FILE, "resourcesPrefix": DRAFT_PREFIX},
            upload=("file", DRAFT_FILE, text.encode("utf-8"),
                    "application/json"))
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


def settle(portal, item_id, surface, original, wanted):
    """After a write raised, say what the surface holds now.

    Returns "landed" (it holds the edits), "untouched" (it holds the old copy)
    or "unknown" (the read failed, or it holds something else). A write that
    times out or loses its reply may still have been stored.
    """
    try:
        now = read_surface(portal, item_id, surface)
    except PortalError:
        return "unknown"
    if same_loose(now, wanted):
        return "landed"
    if same_loose(now, original):
        return "untouched"
    return "unknown"


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
            outcome = settle(portal, item_id, surface, originals[surface],
                             decision.docs[surface])
            if outcome == "landed":
                say("  but a re-read shows the %s holds the edits, so the "
                    "write counts as done" % surface)
            elif outcome == "untouched":
                roll_back(portal, item_id, info, done, originals, say)
                return EXIT_WRITE_FAILED
            else:
                say("  UNKNOWN: the %s may or may not have been written, and "
                    "nothing was rolled back, because a guess could leave "
                    "a published fix with a stale draft. Read both surfaces "
                    "by hand. The old copies are in %s."
                    % (surface, backup_dir))
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
        say("To undo, run the same command again with --edits \"%s\" and a "
            "NEW empty --backup-dir (the old one is refused), plus --apply."
            % os.path.join(backup_dir, "inverse.json"))
        return EXIT_UNVERIFIED
    say("VERDICT: WRITTEN AND VERIFIED on both surfaces (read %d time(s))"
        % attempt)
    return EXIT_OK


# ------------------------------------------------------------------ self-test

_STRIP_STYLE = re.compile(r"\sstyle=(?:\"[^\"]*\"|'[^']*')")
_STRIP_SRC = re.compile(r"\bsrc=(?:\"[^\"]+\"|'[^']+')")


def _sanitize(node):
    """What the stub portal does to a text= upload: drop style=, blank src."""
    if isinstance(node, str):
        return _STRIP_SRC.sub("src=''", _STRIP_STYLE.sub("", node))
    if isinstance(node, list):
        return [_sanitize(v) for v in node]
    if isinstance(node, dict):
        return dict((k, _sanitize(v)) for k, v in node.items())
    return node


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

        def _cut(self):
            """Promise 500 bytes, send 14, hang up: http.client IncompleteRead."""
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "500")
            self.end_headers()
            self.wfile.write(b'{"success": tr')

        def _handle(self, method, fields):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            stub.reqs.append({"method": method, "path": path,
                              "has_ts": "_ts" in fields,
                              "cache": self.headers.get("Cache-Control"),
                              "referer": self.headers.get("Referer"),
                              "query": parsed.query,
                              "ctype": self.headers.get("Content-Type"),
                              "upload": fields.get("_upload"),
                              "keys": sorted(fields),
                              "text": fields.get("text")})
            for prefix in ("/redir", "/html", "/err", "/echo", "/nan",
                           "/dup"):
                if path.startswith(prefix + "/"):
                    path = path[len(prefix):]
                    if prefix == "/redir":
                        return self._send(302, b"", extra={"Location": path})
                    if prefix == "/html":
                        return self._send(200, b"<html>", "text/html")
                    if prefix == "/nan":
                        return self._send(200, b'{"a": NaN}')
                    if prefix == "/dup":
                        return self._send(200, b'{"a": 1, "a": 2}')
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
                if stub.cut_data_gets > 0:
                    stub.cut_data_gets -= 1
                    return self._cut()
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
                if mode in ("ok", "lost", "cut"):
                    stub.data = json.loads(fields["text"])
                if mode == "weird":
                    stub.data = {"weird": True}
                if mode == "lost":
                    stub.cut_data_gets = stub.cut_after_update
                if mode in ("lost", "weird"):
                    return self._send(504, b"gateway timeout", "text/plain")
                if mode == "cut":
                    return self._cut()
                return self._send(200, {"success": True, "id": item})
            if folder or fields.get("fileName") != "config.json" or \
                    fields.get("resourcesPrefix") != "config":
                return self._error(404, "Resource does not exist")
            mode = stub.next_mode(stub.resource_modes)
            if mode == "http500":
                return self._send(500, b"boom", "text/plain")
            if mode in ("ok", "lost"):
                stub.draft_prev = stub.draft
                stub.stale_left = stub.stale_reads
                # The portal strips markup from a text= upload, as ArcGIS
                # Online was seen to do; strip_all strips a file as well.
                upload = fields.get("_upload")
                new = json.loads(fields["file"] if upload else fields["text"])
                if stub.strip_all or (stub.strip_text and not upload):
                    new = _sanitize(new)
                stub.draft = new
                if stub.normalise:
                    stub.draft["widgets"]["widget_2"]["config"]["note"] += " "
            if mode == "lost":
                return self._send(504, b"gateway timeout", "text/plain")
            return self._send(200, {"success": True, "itemId": item,
                                    "owner": owner, "folder": folder})

        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._handle("GET", dict((k, v[0]) for k, v in query.items()))

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            ctype = self.headers.get("Content-Type", "")
            fields = {}
            if ctype.startswith("multipart/form-data"):
                message = email.parser.BytesParser().parsebytes(
                    b"Content-Type: " + ctype.encode("ascii") + b"\r\n\r\n"
                    + raw)
                for part in message.get_payload():
                    name = part.get_param("name", header="content-disposition")
                    fields[name] = part.get_payload(decode=True).decode("utf-8")
                    if part.get_filename():
                        fields["_upload"] = (name, part.get_filename(),
                                             part.get_content_type())
            else:
                body = urllib.parse.parse_qs(raw.decode("ascii"))
                fields = dict((k, v[0]) for k, v in body.items())
            self._handle("POST", fields)

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
        self.strip_text = False
        self.strip_all = False
        self.cut_data_gets = 0
        self.cut_after_update = 0
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


def _styled_fixture():
    """_fixture plus inline styles and an image source on both surfaces."""
    data, draft = _fixture()
    for doc in (data, draft):
        doc["widgets"]["widget_2"]["config"]["note"] += \
            "<p style=\"color:#c00\">warn</p>"
        doc["widgets"]["widget_4"] = {
            "id": "widget_4",
            "config": {"logo": "<img src='logo.png' style='width:9px'>"}}
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


@contextlib.contextmanager
def _isolated_env():
    """Hide the user's real ARCGIS_TOKEN while the self-test runs.

    The command line tests call main(), which reads the environment. A token
    left in it would flip the "no token" cases and reach the loopback stub.
    """
    saved = os.environ.pop("ARCGIS_TOKEN", None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ["ARCGIS_TOKEN"] = saved


def self_test():
    with _isolated_env():
        return _self_test()


def _self_test():
    """Assertions over the decision core, then every network path on a local stub."""
    harness = _Harness()
    check = harness.check
    raises = harness.raises

    def edits_of(raw):
        return parse_edits(json.dumps(raw))

    captured = []        # every line any run printed, for the token search

    def run(stub, tmp, edits=None, name="b", retries=3,
            apply_=True, allow_divergent=False, token=None, root=None):
        lines = []
        portal = Portal(root or stub.url,
                        stub.token if token is None else token, timeout=5)
        code = execute(portal, stub.item_id, edits_of(edits or TITLE_EDIT),
                       apply_, os.path.join(tmp, name), allow_divergent,
                       retries, 0.0, lines.append)
        captured.append("\n".join(lines))
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
    check(same_loose(8, 8.0) and same_loose({"a": [1.0, 2]}, {"a": [1, 2.0]})
          and not same_loose(8, 8.5) and not same_loose(1, True)
          and not same_loose(0, False),
          "same_loose reads 8 and 8.0 as one value, nested, and nothing else  "
          "<-- pinned defect")
    check(loads('{"a": [1, 2]}') == {"a": [1, 2]}, "loads reads plain JSON")
    raises(lambda: loads('{"a": NaN}'), "NaN is refused by loads  "
           "<-- pinned defect")
    raises(lambda: loads('[Infinity]'), "Infinity is refused by loads")
    raises(lambda: loads('{"a": 1, "a": 2}'),
           "a repeated key is refused by loads, not resolved to the last  "
           "<-- pinned defect")

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
    raises(lambda: parse_edits('[{"path": "/a", "expect": NaN, "value": 1}]'),
           "NaN in an edits file raises, so invalid JSON is never written  "
           "<-- pinned defect")
    raises(lambda: parse_edits('[{"path": "/a", "expect": 1, "value": 2, '
                               '"value": 3}]'),
           "a repeated key in an edits file raises instead of keeping the "
           "last  <-- pinned defect")
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
    state, problems, new = plan_surface({"t": 8}, edits_of(
        [{"path": "/t", "expect": 8.0, "value": 9}]), "data")
    check(state == "apply" and not problems and new == {"t": 9},
          "an expect of 8.0 matches a surface that holds 8")

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
    hard, text = diff_docs({"zoom": 8.0, "l": [1.0]}, {"zoom": 8, "l": [1]},
                           set())
    check(hard == [] and text == [],
          "a number the portal normalised from 8.0 to 8 is not drift  "
          "<-- pinned defect")
    hard, text = diff_docs({"a": True}, {"a": 1}, set())
    check(hard == [("a",)], "true against 1 is still a hard difference")
    note = "<p style=\"a\" style='b'>x</p><img src=''><img src=\"\"><img src='k'>"
    check(markup_marks({"n": note, "z": [note], "k": 5}, set()) == (4, 4),
          "markup_marks counts style= and empty src in nested strings")
    check(markup_marks({"n": note, "z": [note]}, set([("z",)])) == (2, 2),
          "markup_marks leaves out the skipped paths")
    check(lost_markup((3, 0), (3, 0)) == "" and lost_markup((3, 0), (4, 0)) == ""
          and lost_markup((3, 1), (3, 0)) == "",
          "more styles or fewer blank sources is not lost markup")
    check("2 against 3" in lost_markup((3, 0), (2, 0))
          and "1 against 0" in lost_markup((3, 0), (3, 1)),
          "fewer styles, or more blank sources, is lost markup")

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
    early = decide({"type": "Web Map", "owner": "u"}, {}, {}, edits_of(TITLE_EDIT))
    check(len(early.problems) == 1 and early.states == {} and early.docs == {},
          "a wrong item type stops the decision before the surfaces are read")
    same_docs = decide(info, data, data, edits_of(TITLE_EDIT))
    check(same_docs.notes == [] and not same_docs.problems,
          "identical surfaces give no note at all")
    check("/widgets/widget_2/config/note" in ok.notes[0],
          "the text note lists the paths, so a changed id or url is visible  "
          "<-- pinned defect")
    s_data, s_draft = _styled_fixture()
    bare = _sanitize(s_draft)
    hurt = decide(info, s_data, bare, edits_of(TITLE_EDIT))
    check(len(hurt.problems) == 1 and "less markup" in hurt.problems[0]
          and "text=" in hurt.problems[0],
          "a draft that lost its style= attributes is refused, not treated "
          "as markup noise  <-- pinned defect")
    hurt = decide(info, s_data, bare, edits_of(TITLE_EDIT), True)
    check(not hurt.problems and any("less markup" in n for n in hurt.notes),
          "--allow-divergent turns the lost-markup refusal into a note")

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
          "text rewritten outside the edits, markup intact, is a note, not a "
          "failure")
    check("(/r)" in n[0], "and the note names the path")
    p, n = verify_surface("draft", {"t": "new", "r": "<p style=\"x\">a</p>"},
                          {"t": "new", "r": "<p>a</p>"}, edits_of(
        [{"path": "/t", "expect": "old", "value": "new"}]))
    check(len(p) == 1 and "stripped markup" in p[0],
          "a style= lost outside the edits fails verification, it is not a "
          "note  <-- pinned defect")
    p, n = verify_surface("data", {"t": 9.0, "r": 8.0}, {"t": 9, "r": 8},
                          edits_of([{"path": "/t", "expect": 1, "value": 9.0}]))
    check(p == [] and n == [],
          "a portal that writes 8.0 as 8 passes verification  "
          "<-- pinned defect")

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
    for bad in ("https://org.example.com/ ", "https://org.example.com/a b",
                "https://org.example.com/\n", "https://org.example.com/\u00e9",
                "https://org.example.com:abc", "https://org.example.com:99999"):
        check(check_portal(bad) is not None,
              "portal %s is refused  <-- pinned defect" % ascii(bad))
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
        with io.open(tf, "w", encoding="utf-8-sig") as handle:
            handle.write(u"bomtoken\n")
        check(load_token(tf, {}) == "bomtoken",
              "a token file saved with a byte order mark is read without it  "
              "<-- pinned defect")
        check(load_token(tf, {"ARCGIS_TOKEN": "env"}) == "bomtoken",
              "--token-file wins over ARCGIS_TOKEN")
        with io.open(tf, "w", encoding="utf-16") as handle:
            handle.write(u"utf16token")
        raises(lambda: load_token(tf, {}), "a UTF-16 token file raises, it "
               "is not read as garbage")

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
        for flag in ("--timeout", "--retry-delay"):
            for value in ("nan", "inf"):
                check(usage_problem(_parse(base + [flag, value]), "")
                      is not None, "%s %s is a usage error  "
                      "<-- pinned defect" % (flag, value))
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
            code, text = run(stub, tmp, name="no op dir", retries=2)
            check(code == 3 and "WRITTEN BUT NOT VERIFIED" in text
                  and "UNVERIFIED: data:" in text
                  and "inverse.json" in text,
                  "a write the portal acknowledges but did not store is caught  "
                  "<-- pinned defect")
            hint = '--edits "%s" and a NEW empty --backup-dir' % os.path.join(
                tmp, "no op dir", "inverse.json")
            check(hint in text,
                  "the undo hint quotes the inverse file, so a path with a "
                  "space can be pasted, and asks for a NEW backup folder  "
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
            ghost.call = lambda path, fields=None, upload=None: [
                "not", "an", "object"]
            raises(lambda: read_all(ghost, ident), "an item record that is not "
                   "an object raises", PortalError)
            ghost.call = lambda path, fields=None, upload=None: (
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
            code, text = run(stub, tmp, name="pub2", apply_=False, token="",
                             root=stub.url + "/err")
            check(code == 4 and "Not permitted" in text,
                  "a portal error with no token to hide is printed as it is")
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

            def flaky(path, fields=None, upload=None):
                if fields is None and "data" in path and path.endswith("/data"):
                    state["n"] += 1
                    if state["n"] >= 3:
                        raise PortalError("read-back failed")
                return real(path, fields, upload)

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

            def flaky_second(path, fields=None, upload=None):
                if fields is None and path.endswith("/data"):
                    state["n"] += 1
                    if state["n"] == 2:
                        raise PortalError("re-read failed")
                return real(path, fields, upload)

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

        # ---- the draft goes up as a FILE, because a text= upload loses markup
        s_data, s_draft = _styled_fixture()
        want_draft = copy.deepcopy(s_draft)
        want_draft["widgets"]["widget_1"]["config"]["title"] = "Park Map"
        stub = _Stub(s_data, s_draft)
        stub.strip_text = True
        try:
            code, text = run(stub, tmp, name="up1")
            sent = [r for r in stub.reqs if r["method"] == "POST"]
            check(code == 0 and sent[0]["upload"]
                  == ("file", "config.json", "application/json")
                  and sent[0]["ctype"].startswith("multipart/form-data")
                  and "text" not in sent[0]["keys"],
                  "the draft is uploaded as a multipart file, never as text=  "
                  "<-- pinned defect")
            check(same(stub.draft, want_draft),
                  "a portal that strips markup from text= uploads leaves the "
                  "draft intact, because the tool sends a file  "
                  "<-- pinned defect")
            check(sent[1]["upload"] is None and "text" in sent[1]["keys"]
                  and all(r["query"] == "" and "token" in r["keys"]
                          for r in sent),
                  "the item data goes up as text=, and both writes carry the "
                  "token in the body and never in the URL query  "
                  "<-- pinned defect")
        finally:
            stub.close()
        stub = _Stub(s_data, s_draft)
        stub.strip_text = True
        rich = [{"path": "/widgets/widget_2/config/note",
                 "expect_data": s_data["widgets"]["widget_2"]["config"]["note"],
                 "expect_draft": s_draft["widgets"]["widget_2"]["config"]["note"],
                 "value_data": "<p style=\"color:#009\">new</p>",
                 "value_draft": "<p style=\"color:#009\">new</p>"}]
        try:
            code, text = run(stub, tmp, edits=rich, name="up2")
            check(code == 0 and stub.draft["widgets"]["widget_2"]["config"]
                  ["note"] == "<p style=\"color:#009\">new</p>",
                  "a rich-text edit that carries style= reaches the draft "
                  "with the style  <-- pinned defect")
        finally:
            stub.close()
        stub = _Stub(s_data, s_draft)
        stub.strip_all = True
        try:
            code, text = run(stub, tmp, name="up3", retries=2)
            check(code == 3 and "UNVERIFIED: draft: the portal stripped "
                  "markup outside the edits (style= attributes" in text
                  and "VERDICT: WRITTEN BUT NOT VERIFIED" in text,
                  "a portal that strips markup even from a file ends at exit "
                  "3, not at VERDICT: WRITTEN AND VERIFIED  "
                  "<-- pinned defect")
        finally:
            stub.close()
        stub = _Stub(s_data, _sanitize(s_draft))
        try:
            code, text = run(stub, tmp, name="up4")
            check(code == 1 and stub.posts() == [] and "less markup" in text,
                  "a draft that an earlier text= write already stripped is "
                  "refused before any write  <-- pinned defect")
            code, text = run(stub, tmp, name="up5", allow_divergent=True)
            check(code == 0 and "less markup" in text,
                  "--allow-divergent writes over a stripped draft and says so")
        finally:
            stub.close()

        # ---- a write whose reply is lost may still have been stored
        data, draft = _fixture()
        stub = _Stub(data, draft)
        stub.update_modes = ["lost"]
        try:
            code, text = run(stub, tmp, name="lost1")
            check(code == 0 and "FAILED writing data" in text
                  and "counts as done" in text and "restored" not in text
                  and stub.draft["widgets"]["widget_1"]["config"]["title"]
                  == "Park Map" and stub.data["widgets"]["widget_1"]["config"]
                  ["title"] == "Park Map",
                  "a published write that was stored but lost its reply is "
                  "counted as done and the draft is NOT rolled back  "
                  "<-- pinned defect")
        finally:
            stub.close()
        stub = _Stub(data, draft)
        stub.resource_modes = ["lost"]
        try:
            code, text = run(stub, tmp, name="lost2")
            check(code == 0 and "FAILED writing draft" in text
                  and "counts as done" in text
                  and stub.posts() == ["updateResources", "update"],
                  "a draft write that was stored but lost its reply is "
                  "counted as done and the published write follows")
        finally:
            stub.close()
        stub = _Stub(data, draft)
        stub.update_modes = ["lost"]
        stub.cut_after_update = 5
        try:
            code, text = run(stub, tmp, name="lost3")
            check(code == 2 and "UNKNOWN" in text and "restored" not in text
                  and stub.posts() == ["updateResources", "update"]
                  and stub.draft["widgets"]["widget_1"]["config"]["title"]
                  == "Park Map",
                  "a lost reply whose surface cannot be read back is UNKNOWN: "
                  "nothing is rolled back and the user is told to look  "
                  "<-- pinned defect")
        finally:
            stub.close()
        stub = _Stub(data, draft)
        stub.update_modes = ["weird"]
        try:
            code, text = run(stub, tmp, name="lost4")
            check(code == 2 and "UNKNOWN" in text and "restored" not in text
                  and stub.posts() == ["updateResources", "update"],
                  "a surface that holds neither the old nor the new copy is "
                  "UNKNOWN and is not rolled back")
        finally:
            stub.close()

        stub = _Stub(data, draft)
        stub.update_modes = ["cut"]
        try:
            code, text = run(stub, tmp, name="cut2")
            check(code == 0 and "FAILED writing data" in text
                  and "IncompleteRead" in text and "counts as done" in text,
                  "a write reply cut short is a PortalError, the surface is "
                  "read again, and the stored write counts as done  "
                  "<-- pinned defect")
        finally:
            stub.close()

        # ---- a reply cut short is a read failure, not a traceback
        stub = _Stub(data, draft)
        stub.cut_data_gets = 1
        try:
            code, text = run(stub, tmp, name="cut1", apply_=False)
            check(code == 4 and "IncompleteRead" in text,
                  "a reply cut short is a read failure at exit 4, not a "
                  "traceback at exit 1  <-- pinned defect")
        finally:
            stub.close()

        # ---- the number of read-backs
        stub = _Stub(data, draft)
        try:
            code, text = run(stub, tmp, name="rb1", retries=1)
            check(code == 0 and "read 1 time(s)" in text,
                  "one read-back is enough to verify, --retries 1 works  "
                  "<-- pinned defect")
        finally:
            stub.close()
        stub = _Stub(data, draft)
        stub.stale_reads = 1
        try:
            code, text = run(stub, tmp, name="rb2", retries=1)
            check(code == 3, "one read-back that is stale ends at exit 3")
        finally:
            stub.close()
        stub = _Stub(data, draft)
        stub.stale_reads = 2
        sleeps = []
        real_sleep = time.sleep
        time.sleep = sleeps.append
        try:
            code = execute(Portal(stub.url, stub.token, 5), stub.item_id,
                           edits_of(TITLE_EDIT), True,
                           os.path.join(tmp, "rb3"), False, 3, 7.5,
                           captured.append)
        finally:
            time.sleep = real_sleep
            stub.close()
        check(code == 0 and sleeps == [7.5, 7.5],
              "the tool waits --retry-delay seconds between read-backs")

        # ---- replies that are not plain JSON
        stub = _Stub(data, draft)
        try:
            code, text = run(stub, tmp, name="nan1", root=stub.url + "/nan")
            check(code == 4 and "did not return JSON" in text,
                  "a reply holding NaN is a read failure")
            code, text = run(stub, tmp, name="dup1", root=stub.url + "/dup")
            check(code == 4 and "did not return JSON" in text,
                  "a reply with a repeated key is a read failure, so the "
                  "tool never writes back a copy that lost a value")
        finally:
            stub.close()
        check(not _is_portal_error({"error": {"code": 5}, "widgets": {}})
              and not _is_portal_error({"error": {"mode": "x"}})
              and not _is_portal_error({"error": {"code": True}})
              and not _is_portal_error({"error": "text"})
              and not _is_portal_error([{"error": {"code": 5}}])
              and _is_portal_error({"error": {"code": 498, "message": "m"}}),
              "only the one ArcGIS error shape counts as a portal error")
        quirk, quirk_draft = _fixture()
        quirk["error"] = quirk_draft["error"] = {"code": "none", "text": "x"}
        stub = _Stub(quirk, quirk_draft)
        try:
            code, text = run(stub, tmp, name="errkey", apply_=False)
            check(code == 0 and "VERDICT: ALLOW" in text,
                  "an app config with its own top-level error key is read as "
                  "data, not as a portal error  <-- pinned defect")
        finally:
            stub.close()

        # ---- tokens with characters that need percent-encoding
        odd = "a+b/c=d&e%f"
        stub = _Stub(data, draft, token=odd)
        try:
            code, text = run(stub, tmp, name="odd1")
            check(code == 0 and "WRITTEN AND VERIFIED" in text,
                  "a token with + / = & and % characters authenticates")
            shaped = Portal(stub.url + "/my portal", odd, timeout=5)
            heard = []

            def probe():
                try:
                    shaped.call("content/items/x")
                except PortalError as exc:
                    heard.append(str(exc))
                    raise

            raises(probe, "a request line with a space is a PortalError, not "
                   "a traceback  <-- pinned defect", PortalError)
            message = heard[0]
            check("can't contain control characters" in message,
                  "http.client refuses a request line with a space")
            check(odd not in message
                  and urllib.parse.quote_plus(odd) not in message
                  and urllib.parse.quote(odd, safe="") not in message
                  and "***" in message,
                  "the token is removed from that error in its raw and its "
                  "percent-encoded form  <-- pinned defect")
            captured.append(message)
        finally:
            stub.close()

        # ---- a proxy in the environment is not used for loopback
        saved = dict((k, os.environ.get(k)) for k in
                     ("HTTP_PROXY", "HTTPS_PROXY"))
        os.environ["HTTP_PROXY"] = os.environ["HTTPS_PROXY"] = \
            "http://127.0.0.1:9"
        stub = _Stub(data, draft)
        try:
            code, text = run(stub, tmp, name="px", apply_=False)
            local = [h for h in Portal(stub.url).opener.handlers
                     if isinstance(h, urllib.request.ProxyHandler)]
            remote = [h for h in Portal("https://org.example.com").opener
                      .handlers if isinstance(h, urllib.request.ProxyHandler)]
            check(code == 0 and local == []
                  and "https" in remote[0].proxies,
                  "loopback never goes through a proxy, a real portal keeps "
                  "the system proxy  <-- pinned defect")
        finally:
            stub.close()
            for key in saved:
                os.environ.pop(key, None)
            os.environ.update(dict((k, v) for k, v in saved.items()
                                   if v is not None))

        # ---- the self-test hides the user's real token
        os.environ["ARCGIS_TOKEN"] = "ambient-canary"
        try:
            with _isolated_env():
                inside = os.environ.get("ARCGIS_TOKEN")
            after = os.environ.get("ARCGIS_TOKEN")
        finally:
            del os.environ["ARCGIS_TOKEN"]
        check(inside is None and after == "ambient-canary",
              "the self-test removes ARCGIS_TOKEN while it runs and puts it "
              "back  <-- pinned defect")

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
            def cli(extra, url=None):
                argv = ["--portal", url or stub.url, "--item", stub.item_id,
                        "--edits", ef, "--timeout", "5", "--retry-delay", "0"]
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    code = main(argv + extra)
                captured.append(out.getvalue() + err.getvalue())
                return code, out.getvalue(), err.getvalue()

            code, out, err = cli(["--token-file", tf])
            check(code == 0 and "VERDICT: ALLOW" in out and stub.posts() == [],
                  "the command line check run works with a token file")
            nowrite = os.path.join(tmp, "nowrite")
            code, out, err = cli(["--token-file", tf, "--backup-dir", nowrite])
            check(code == 0 and "VERDICT: ALLOW" in out and stub.posts() == []
                  and not os.path.exists(nowrite),
                  "--backup-dir without --apply writes nothing and makes no "
                  "folder  <-- pinned defect")
            os.environ["ARCGIS_TOKEN"] = "wrong-env-token"
            try:
                code, out, err = cli(["--token-file", tf])
            finally:
                del os.environ["ARCGIS_TOKEN"]
            check(code == 0 and "VERDICT: ALLOW" in out,
                  "--token-file wins over ARCGIS_TOKEN on the command line")
            os.environ["ARCGIS_TOKEN"] = stub.token
            try:
                code, out, err = cli([])
            finally:
                del os.environ["ARCGIS_TOKEN"]
            check(code == 0 and "VERDICT: ALLOW" in out,
                  "ARCGIS_TOKEN works when there is no token file")
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
            wrote = [r for r in stub.reqs if r["method"] == "POST"]
            check(len(wrote) == 2 and all(r["query"] == "" for r in wrote),
                  "the command line writes carry no URL query at all")
            with io.open(tf, "w", encoding="utf-8-sig") as handle:
                handle.write(stub.token)
            code, out, err = cli(["--token-file", tf])
            check(code == 0 and "NOTHING TO DO" in out,
                  "a token file saved with a byte order mark authenticates  "
                  "<-- pinned defect")
            with io.open(ef, "w", encoding="utf-8-sig") as handle:
                handle.write(json.dumps(TITLE_EDIT))
            code, out, err = cli(["--token-file", tf])
            check(code == 0 and "NOTHING TO DO" in out,
                  "an edits file saved with a byte order mark is read")
            code, out, err = cli(["--token-file", tf, "--timeout", "nan"])
            check(code == 64 and "real numbers" in err,
                  "--timeout nan is a usage error on the command line  "
                  "<-- pinned defect")
            data2, draft2 = _fixture()
            extra2 = copy.deepcopy(draft2)
            extra2["widgets"]["widget_3"] = {"id": "widget_3"}
            dstub = _Stub(data2, extra2)
            try:
                code, out, err = cli(["--token-file", tf, "--apply",
                                      "--backup-dir", os.path.join(tmp, "cli-dv")],
                                     dstub.url)
                check(code == 1 and "unpublished" in out
                      and dstub.posts() == [],
                      "the command line refuses a draft that holds unpublished "
                      "work  <-- pinned defect")
                code, out, err = cli(["--token-file", tf, "--apply",
                                      "--allow-divergent", "--backup-dir",
                                      os.path.join(tmp, "cli-dv2")], dstub.url)
                check(code == 0 and dstub.posts() == ["updateResources",
                                                      "update"]
                      and "widget_3" in dstub.draft["widgets"],
                      "the command line --allow-divergent writes and keeps "
                      "the unpublished work")
            finally:
                dstub.close()
            real_execute = globals()["execute"]

            def boom(*args, **kwargs):
                raise RuntimeError("probe %s" % stub.token)

            globals()["execute"] = boom
            try:
                code, out, err = cli(["--token-file", tf])
            finally:
                globals()["execute"] = real_execute
            check(code == 70 and "unexpected RuntimeError" in err
                  and "Traceback" in err and "0 write request(s)" in err
                  and stub.token not in err and "***" in err,
                  "a fault in the tool exits 70, not the 1 that means "
                  "refused, and scrubs the token  <-- pinned defect")
            seen = []

            class Reconfigurable(io.StringIO):
                def reconfigure(self, **kw):
                    seen.append(kw)

            with contextlib.redirect_stdout(Reconfigurable()), \
                    contextlib.redirect_stderr(Reconfigurable()):
                main([])
            check(seen == [{"errors": "replace"}] * 2,
                  "main makes stdout and stderr replace what they cannot "
                  "encode")
            uni = os.path.join(tmp, "uni.json")
            with io.open(uni, "w", encoding="utf-8") as handle:
                handle.write(u'[{"path": "/caf\u00e9", "expect": 1, '
                             u'"value": 2}]')
            child = subprocess.run(
                [sys.executable, os.path.abspath(__file__), "--portal",
                 stub.url, "--item", stub.item_id, "--edits", uni,
                 "--token-file", tf],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
                env=dict(os.environ, PYTHONIOENCODING="ascii",
                         PYTHONUTF8="0"))
            captured.append(child.stdout.decode("ascii", "replace"))
            check(child.returncode == 1 and b"/caf? is absent" in child.stdout
                  and b"Traceback" not in child.stderr,
                  "a path the console cannot encode prints as ? and the "
                  "verdict still arrives, instead of a traceback and exit 1  "
                  "<-- pinned defect")
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
        everything = "\n".join(captured) + "\n".join(files)
        check(secret not in everything,
              "the token is in no output and in no file written  "
              "<-- pinned defect")
        check(odd not in everything
              and urllib.parse.quote_plus(odd) not in everything,
              "a token with percent-encoded characters is in no output "
              "either, raw or encoded  <-- pinned defect")
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


def _tolerate_odd_characters():
    """Make print() unable to fail on a character the console cannot encode.

    A redirected stdout on Windows is cp1252, and a portal message or a config
    key may hold a character it cannot write. The UnicodeEncodeError would
    arrive after the backup or even after the write, with the verdict unprinted.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")


def main(argv=None):
    """Run the CLI. A fault in the tool exits 70, because 1 means refused."""
    _tolerate_odd_characters()
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
    try:
        return execute(portal, args.item, edits, args.apply, args.backup_dir,
                       args.allow_divergent, args.retries, args.retry_delay,
                       print)
    except Exception as exc:
        print(portal._clean("error: unexpected %s: %s. This is a fault in the "
                            "tool, not a finding. %d write request(s) had "
                            "been sent; if that is not 0, read both surfaces "
                            "by hand." % (type(exc).__name__, exc,
                                          portal.posts)), file=sys.stderr)
        print(portal._clean(traceback.format_exc()), file=sys.stderr)
        return EXIT_FAULT


if __name__ == "__main__":
    sys.exit(main())
