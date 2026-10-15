# exbtwin

Write an Experience Builder fix to both of the app's config surfaces, refuse when either one
is not what you expect, and read both back afterwards.

An analyst finds that the search widget in a published Experience Builder app points at a layer
that was renumbered. She fixes it over REST: she updates the item data, reloads the app, and the
search works. She closes the ticket. Two weeks later a colleague opens the builder to change a
title and presses Publish. The search widget is broken again. Nobody sees an error, because
nothing failed. The builder did what it was built to do.

The app keeps its configuration twice. The item data (`/content/items/<id>/data`) is the
published copy that visitors load. The resource `resources/config/config.json` is the builder's
draft. The builder edits the draft, and Publish copies the draft over the item data. Esri's
`solution.js` project documents the pairing: the draft is stored in `config/config.json`, and
publishing pushes the changes to the item data
([issue 660](https://github.com/Esri/solution.js/issues/660)). A fix to the item data alone lasts
until the next Publish. A fix to the draft alone is invisible until the next Publish. Both
failures look like success on the day.

A third failure looks like success too. ArcGIS Online has been seen to strip every `style=`
attribute and blank every image source when the draft is written as `text=`. The app then serves
plain text to every visitor, and the write reported `success`. Esri does not document this
behaviour. The tool therefore uploads the draft as a multipart file, and it fails the run when
markup disappears.

This tool writes one edit to both surfaces and then proves it. It refuses before writing when a
surface does not hold the value you expected to replace. It refuses when the draft and the
published copy differ in structure, which means somebody has unpublished work in the builder. It
refuses when the draft holds less markup than the published copy. It writes the draft first and
the item data second. It then reads both surfaces again, with the cache defeated, and exits
non-zero unless every edited value reads back, no `style=` attribute or image source was lost, and
nothing else moved.

This is a real run against the self-test stub, with the port left out. The stub is a local
`http.server`, not a portal. The first command checks, the second writes, the third runs again
and finds nothing to do:

```
$ python exbtwin.py --portal http://127.0.0.1:PORT --item 0123456789abcdef0123456789abcdef --edits edits.json --token-file token.txt
  note: 1 text value(s) differ between the surfaces outside the edits (/widgets/widget_2/config/note); the builder rewrites markup, so this is not treated as drift. A changed id, url or label would pass here too, so read the list
  draft: will change 1 edit(s)
  data: will change 1 edit(s)
VERDICT: ALLOW
Check only. Nothing was written. Re-run with --apply and --backup-dir to write: draft, then data.
$ python exbtwin.py ... --apply --backup-dir ./backup
  note: 1 text value(s) differ between the surfaces outside the edits (/widgets/widget_2/config/note); the builder rewrites markup, so this is not treated as drift. A changed id, url or label would pass here too, so read the list
  draft: will change 1 edit(s)
  data: will change 1 edit(s)
=== APPLY ===
  backup kept in ./backup (data.json, draft.json, inverse.json)
  wrote draft
  wrote data
VERDICT: WRITTEN AND VERIFIED on both surfaces (read 1 time(s))
$ python exbtwin.py ... --apply --backup-dir ./backup2
  note: 1 text value(s) differ between the surfaces outside the edits (/widgets/widget_2/config/note); the builder rewrites markup, so this is not treated as drift. A changed id, url or label would pass here too, so read the list
  draft: already holds the edits
  data: already holds the edits
VERDICT: NOTHING TO DO. Both surfaces already hold the edits.
```

## The existing alternatives

Two public repositories come close. I searched GitHub on 2026-10-15 with `gh search repos` for
`exbtwin`, `experience builder`, `ArcGisPortalEXBTemplate`, `experience builder config backup json`
and `experience builder publish draft config.json`. Only this repository is called exbtwin. Neither
repository below does the job of this tool, and neither claims to.

[exb-publish-agol](https://github.com/teeks822828/exb-publish-agol) (MIT) is a Claude Code skill
that builds Experience Builder apps on ArcGIS Online by script, from cached donor configs. It
ships a validator for the config traps its author found and a guard that refuses to write to
anything but an ArcGIS Online organization. Its `exb_tools.py` pulls both stores and reports
whether they differ. Its push backs up both stores, uploads the draft as a file, and then updates
the item data. If you want to author a new app, or add pages, charts and tables to one, use it.
This tool does not build anything.

[braywilliam01/ArcGisPortalEXBTemplate](https://github.com/braywilliam01/ArcGisPortalEXBTemplate)
(no licence file) describes itself as "Exporting Experience Builder Config for backups or JSON
Editing workflows". It is an ArcGIS Pro notebook that pulls an app's config by item id, lets you
review and edit the file, and pushes it back to a staging portal. Use it when you want a reviewed
copy of a whole template under version control.

The jobs differ. This tool edits values that already exist, in an app that is already published,
and it checks the result. As read for this README (the first one's `README.md`, `SKILL.md` and
`scripts/exb_tools.py`, the second one's `README.md`), neither describes a push that refuses to
write when the two surfaces differ, a read-back of both surfaces after the write, or a check that
an edit started from the value you expected. Neither was run for this README, so the comparison
stops at what they document.

## What you need

Python 3.9 or newer, and no packages. `--self-test` needs no portal, no token and no network
beyond a stub on `127.0.0.1`. The self-test ran green on Python 3.9.25, 3.12.10 and 3.13.2 on
Windows and on 3.12.3 on Linux, with the same 225 assertions on each.

```
git clone https://github.com/uhsear/exbtwin.git
python exbtwin.py --self-test
```

The self-test hides `ARCGIS_TOKEN` while it runs, so a token in your shell does not change the
result and never reaches the stub.

## Usage

Write an edits file. It is a JSON list. Each edit names a
[JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901) (RFC 6901), the value the path holds now,
and the value to put there:

```json
[
  {"path": "/widgets/widget_1/config/title",
   "expect": "Old title",
   "value": "New title"}
]
```

Check first. Nothing is written without `--apply`.

```
python exbtwin.py --portal https://example.maps.arcgis.com --item 0123456789abcdef0123456789abcdef --edits edits.json
python exbtwin.py --portal https://example.maps.arcgis.com --item 0123456789abcdef0123456789abcdef --edits edits.json --token-file token.txt --backup-dir ./backup --apply
```

A public app needs no token to be checked. `--apply` needs one. The token comes from
`--token-file` (a file holding the token and nothing else) or from the `ARCGIS_TOKEN`
environment variable. The file wins when both are set. No flag takes the token itself, so it never
appears in a process list. A token file may start with a UTF-8 byte order mark. A UTF-16 file is a
usage error.

| Flag | Default | What it does |
|---|---|---|
| `--portal` | none | Portal root, for example `https://example.maps.arcgis.com`. Must be https, except `localhost` and `127.0.0.1`. Plain ASCII only, with no space. Required. |
| `--item` | none | The 32 character item id of the app. Required. |
| `--edits` | none | The edits file. Required. |
| `--token-file` | none | File holding the token. Falls back to `ARCGIS_TOKEN`. |
| `--backup-dir` | none | Folder that keeps `data.json`, `draft.json` and `inverse.json`. Required with `--apply`. |
| `--allow-divergent` | off | Write although the draft holds unpublished structural changes, or less markup than the published copy. |
| `--retries` | `3` | Read-backs before the verification gives up. |
| `--retry-delay` | `2.0` | Seconds between read-backs. A real number, not `nan` or `inf`. |
| `--timeout` | `30` | Seconds per request. A real number, not `nan` or `inf`. |
| `--apply` | off | Perform the write. Without it nothing is written, and no backup folder is made. |
| `--self-test` | off | Run the offline assertions and exit. |

The parser sets `allow_abbrev=False`. `--ap` does not mean `--apply`: it is a usage error
(exit 64), and the self-test pins that.

Exit codes: 0 done, or the check is clean. 1 refused. 2 a write failed. 3 written but not
verified. 4 the app could not be read. 64 usage error. 70 a fault in the tool itself, such as an
exception it did not expect. Exit 70 prints the exception with the token removed and says how many
write requests were sent. Exit 1 covers every refusal, including a backup folder that cannot be
made, so read the `REFUSED` line to tell them apart.

### Edits that differ between the surfaces

Rich-text widgets legitimately hold different markup in the two surfaces. A replacement written
against one surface matches nothing on the other. This tool does not skip the miss. It refuses
the whole run and names the surface. For such an edit, give a value per surface:

```json
[
  {"path": "/widgets/widget_2/config/note",
   "expect_data":  "<ul data-list=\"bullet\"><li>open</li></ul>",
   "expect_draft": "<ul><li>open</li></ul>",
   "value_data":   "<ul data-list=\"bullet\"><li>closed</li></ul>",
   "value_draft":  "<ul><li>closed</li></ul>"}
]
```

`expect_data`, `expect_draft`, `value_data` and `value_draft` override `expect` and `value` for
one surface. Every edit must end up with an `expect` and a `value` for both surfaces. A
misspelt key is an error, and so are two edits whose paths overlap, because their order would
matter. A value may carry `style=` attributes. The draft upload keeps them.

The edits file is strict JSON. `NaN`, `Infinity` and a repeated key in one object are errors,
because the tool would otherwise write invalid JSON, or keep only the last of two equal keys. A
file saved with a byte order mark is read.

## What it decides

Before it writes anything, it reads the item record, the item data and the draft resource, and
it refuses when:

- the item type is not `Web Experience`, the type Esri lists for an app made in Experience Builder
  ([Items and item types](https://developers.arcgis.com/rest/users-groups-and-items/items-and-item-types/));
- a path is absent on either surface. Edits replace values. They never add or delete a key;
- a surface holds neither the `expect` value nor the new value. A surface that already holds the
  new value counts as done, so a rerun after a half finished run writes only what is missing;
- the draft and the published copy differ in keys, list lengths or non-text values outside the
  edited paths. That is unpublished builder work, and the next Publish would ship it. Pass
  `--allow-divergent` after you have looked at it;
- the draft holds fewer `style=` attributes, or more blank `src=''` values, than the published
  copy. An earlier `text=` write may have stripped it, and the next Publish would ship the loss.
  Repair it first: upload the published JSON as the resource file. `--allow-divergent` turns the
  refusal into a note;
- either surface changed between the first read and the moment before the write.

A number the portal writes as `8` instead of `8.0` is the same value. A difference that is only
a string is not drift, because the builder rewrites markup when it saves. The report counts those
strings and lists their paths in a note. Read the list.

When it writes (`--apply`), it:

1. Saves the old item data, the old draft and `inverse.json` in `--backup-dir`. It refuses to
   overwrite a backup, so use an empty folder. `inverse.json` is an edits file.
2. Writes the draft with `updateResources`, as a multipart file upload. Then it writes the item
   data with `update`, as `text=`. The token travels in the request body, never in the URL.
3. Reads both surfaces again, each time with a throwaway `_ts` parameter and
   `Cache-Control: no-cache`. It checks that every edited path holds the new value, that nothing
   else changed in keys, list lengths or non-text values, and that no `style=` attribute or image
   source was lost outside the edits. It retries up to `--retries` times, because the draft
   resource sits behind a CDN.
4. Exits 3 if the read-back never matches. The message names the undo command. To undo, run the
   same command with `--edits "<backup>/inverse.json"`, a new empty `--backup-dir` and
   `--apply`. The old backup folder is refused.

### When a write raises

A timeout, a 5xx answer or a reply cut short does not prove that the portal skipped the write. The
portal may have stored it and lost the reply. After any failed write the tool reads that surface
again and decides from what it finds:

- The surface holds the edits. The write counts as done, the run goes on, and the output says so.
- The surface holds the old copy. The tool writes back the surfaces it already changed, reads
  each one, and exits 2.
- The read fails, or the surface holds something else. The tool rolls nothing back, prints
  `UNKNOWN`, names the backup folder, and exits 2. A rollback on a guess could leave a published
  fix with a stale draft, which is the state this tool exists to prevent. Read both surfaces by
  hand.

### Why the draft goes first

A crash between the two writes leaves one surface changed. Writing the draft first leaves a draft
that is ahead of the published copy. Visitors see the old app until the next Publish, which then
ships the fix. Writing the item data first leaves a published fix with an old draft, and the next
Publish removes the fix without a sign. The first state is the harmless one, so the tool
produces it. The self-test pins the order.

### Why the draft is a file

Esri documents two ways to change an existing resource: a multipart `file`, or `text` used with
`fileName`. The text form has been seen to lose markup on ArcGIS Online. The tool sends the file.
The stub can strip `style=` and blank `src` from every `text=` upload, as ArcGIS Online was seen to do.
The self-test shows that the tool's draft arrives intact, that a portal which strips even a file
fails the run at exit 3, and that a draft already stripped is refused before the write.

## The Esri behaviour it relies on

| Claim | Source | Exercised |
|---|---|---|
| The item data is the published copy and `config/config.json` is the draft. | [solution.js issue 660](https://github.com/Esri/solution.js/issues/660) | Documented, not exercised: the stub is built to this contract. |
| An app is an item of type `Web Experience` with a JSON data section. | [Items and item types](https://developers.arcgis.com/rest/users-groups-and-items/items-and-item-types/) | Stub: a `Web Map` item is refused. |
| `.../content/items/<id>/data` returns the JSON of the data section. | [Item Data](https://developers.arcgis.com/rest/users-groups-and-items/item-data/) | Stub, by every run. |
| `.../content/items/<id>/resources/<name>` returns a resource, and names may hold slashes. | [Item Resources](https://developers.arcgis.com/rest/users-groups-and-items/item-resources/) | Stub, by every run. |
| `update` takes `text` for the item's JSON and answers `{"success": ..., "id": ...}`. | [Update Item](https://developers.arcgis.com/rest/users-groups-and-items/update-item/) | Stub, by every applied run. |
| `updateResources` takes a multipart `file` ("pursuant to IETF RFC1867"), pairs `fileName` with it, requires `resourcesPrefix` to match, and answers `success`. | [Update Resources](https://developers.arcgis.com/rest/users-groups-and-items/update-resources/) | Stub parses the multipart body and checks the file name, content type and fields. |
| `updateResources` also takes `text` together with `fileName`. | The same page | Stub only, to show what a `text=` upload would lose. The tool never sends it. |
| `token` and `f=json` are ordinary request parameters. | [Common parameters](https://developers.arcgis.com/rest/users-groups-and-items/common-parameters/) | Stub: the stub refuses a request without the right token. |
| The item record holds `owner`, and `ownerFolder` for the owner or an administrator. | [Item](https://developers.arcgis.com/rest/users-groups-and-items/item/) | Stub: a record with `ownerFolder` hidden fails the write cleanly. |
| An error is a JSON body `{"error": {"code": 498, "message": "Invalid Token", "details": []}}`. | [Response types](https://developers.arcgis.com/rest/geocode/response-types/) (the geocode service page; the users, groups and items pages show no error body) | Stub, by every read failure. A config that has its own `error` key is read as data. |

Four things the tool does that the pages above do not state:

- **The folder in the update path.** The current Update Item page shows
  `/content/users/<user>/items/<id>/update`. The tool inserts the item's `ownerFolder` when the
  record has one, because the User Content page lists a folder-scoped item resource. That is the
  least documented choice in the tool. Only the stub exercised it. If a portal rejects it, the
  write fails (exit 2) and the draft is restored. The read-back is the safety net.
- **The markup stripping.** No Esri page says that a `text=` upload to `updateResources` is
  sanitised. The tool relies on observation here, and the stub imitates it. A real portal may
  differ, and the read-back is how the tool finds out.
- **The CDN cache on the draft.** No Esri page says that the resource is cached. The cache bust is
  there because a stale read after a write was seen in practice. The stub serves stale reads on
  demand and the self-test covers both a stale read that clears and one that does not.
- **The `Referer` header.** The tool sends the portal URL. Esri's Generate Token page ties a
  token to "the base URL of the client application that will use the token", and does not say
  that a header must match. The header is harmless when it is not needed.

Python behaviour it relies on, from the standard library documentation:
[`ProxyHandler`](https://docs.python.org/3/library/urllib.request.html#urllib.request.ProxyHandler)
reads proxy settings from the environment, so the tool gives loopback addresses an empty one and
leaves a real portal on the system proxy;
the [`utf-8-sig` codec](https://docs.python.org/3/library/codecs.html#module-encodings.utf_8_sig)
drops a byte order mark; and
[`TextIOWrapper.reconfigure`](https://docs.python.org/3/library/io.html#io.TextIOWrapper.reconfigure)
lets the tool print `?` for a character the console cannot encode, instead of failing.

## A real run

```
$ python exbtwin.py --self-test
exbtwin self-test: temporary files and a local stub only, no real portal, no credentials
--------------------------------------------------------------------
PASS  a pointer splits on slashes
...
PASS  a draft that lost its style= attributes is refused, not treated as markup noise  <-- pinned defect
...
PASS  a unique prefix --ap of --apply is REFUSED by the parser  <-- pinned defect
...
PASS  the draft is written BEFORE the published copy  <-- pinned defect
...
PASS  the draft is uploaded as a multipart file, never as text=  <-- pinned defect
PASS  a portal that strips markup from text= uploads leaves the draft intact, because the tool sends a file  <-- pinned defect
...
PASS  a portal that strips markup even from a file ends at exit 3, not at VERDICT: WRITTEN AND VERIFIED  <-- pinned defect
...
PASS  a published write that was stored but lost its reply is counted as done and the draft is NOT rolled back  <-- pinned defect
PASS  a lost reply whose surface cannot be read back is UNKNOWN: nothing is rolled back and the user is told to look  <-- pinned defect
...
PASS  a reply cut short is a read failure at exit 4, not a traceback at exit 1  <-- pinned defect
...
PASS  the token is removed from that error in its raw and its percent-encoded form  <-- pinned defect
...
PASS  the self-test removes ARCGIS_TOKEN while it runs and puts it back  <-- pinned defect
...
PASS  a fault in the tool exits 70, not the 1 that means refused, and scrubs the token  <-- pinned defect
...
PASS  loading the file runs nothing by itself and exposes main
--------------------------------------------------------------------
225 assertions, 0 failed
```

The full run prints all 225 assertions. Each `...` line above is where this block is cut.

The harness is the `check()` and `raises()` pair used across this portfolio. The test is part of
the tool: fixtures are synthetic, and the portal is a stub on `127.0.0.1` that listens for the
exact calls the tool makes. Among the cases pinned as defects:

- a prefix of the write flag (`--ap`) is refused by the parser;
- the draft is written before the published copy;
- the draft is uploaded as a multipart file, and markup lost on either surface exits 3;
- a write the portal acknowledges but did not store (`success: true`, nothing changed) exits 3;
- a write that the portal stored but did not answer is found by a re-read, and the other surface
  is not rolled back on a guess;
- a reply cut short, a portal URL with a space and a fault in the tool each end in a message and
  the right exit code, not a traceback and exit 1;
- a replacement written for one surface is refused on the other instead of silently skipped;
- a portal error that echoes the token is redacted before it is printed, raw or percent-encoded;
- the token reaches no output and no file. The test reads every file the run wrote and every
  line any run printed, and looks for the token. It also checks that a write puts the token in the
  body and never in the URL query;
- a token in `ARCGIS_TOKEN` does not change the self-test.

Branch coverage is measured with `coverage run --branch exbtwin.py --self-test`:

```
Name         Stmts   Miss Branch BrPart  Cover   Missing
--------------------------------------------------------
exbtwin.py    1466      0    350      0   100%
--------------------------------------------------------
```

All 225 assertions give the same output on Windows (Python 3.13) and on Linux (Python 3.12).
Compare the two transcripts after you remove the carriage returns that the Windows console adds:
they are then identical, line by line. Nothing in the test depends on the platform. The one thing
the offline run cannot reach is a real portal, see Limits.

## Limits

- **No live portal.** Every network path ran against a local stub, not against ArcGIS Online or
  ArcGIS Enterprise. The stub follows Esri's published pages and the stripping behaviour described
  above. It cannot prove that a real portal accepts the folder form of the update path, honours the
  `_ts` cache bust or returns the resource byte for byte. The read-back exists for that reason, and
  the first real run should be a check run followed by a single edit on a copy of an app.
- **Whole document writes.** `update` and `updateResources` replace a whole surface. The tool
  reads the surfaces twice and compares them, but a save in the builder between the second read
  and the write is not detected. The same goes for an open builder tab: the builder holds its
  own copy and can save over the draft later. Close the builder, or tell the people who have it
  open, before you use `--apply`.
- **The draft is read through a cache.** The cache bust works only if the portal honours it. A
  builder save made within the cache window can go unseen, and the write can then replace it.
- **A lost reply can leave the answer unknown.** If a write times out, the portal may still have
  applied it. The tool re-reads the surface to find out. If that read also fails, it prints
  `UNKNOWN` and stops. Read both surfaces yourself before you trust any line of that run.
- **A killed run.** If the process is killed or the machine loses the network during `--apply`,
  nothing re-reads the surfaces. Read both before you run again.
- **Replace only.** An edit changes a value that exists. It does not add a widget, a key or a
  list entry, and it does not delete one. A pointer to an empty key (`/` or `/a/`) is refused, and
  the pointer `-` on a list names no element. Building an app is the other tool's job.
- **One whole value per edit.** The tool has no search and replace inside a rich-text string. A
  renumbering that touches many widgets needs one edit per value, so generate the edits file with
  another tool.
- **String drift is a note.** If somebody changes only a text value in the builder and does not
  publish, the tool counts it as markup noise and still writes. A string-only difference in an
  `itemId`, a url, a layer id or a label does not refuse the write either, so unpublished
  rebinding in the builder passes. The note lists the paths. Structure and non-text values
  are checked.
- **The draft must exist.** An app that was never opened in the builder may have no
  `config/config.json`. The read fails (exit 4) and the tool does nothing.
- **An edit can still be wrong.** The tool checks that your value arrived on both surfaces. It
  does not check that the app works. Open the app, and read the `config` for anything that
  matters.
- **Undo restores both surfaces.** The inverse file puts the old value back on both surfaces, also
  on a surface that somebody else had already fixed before the run. An undo run needs its own
  empty `--backup-dir`.
- **Replies must be plain JSON objects from the host you named.** A redirect (even from http to
  https), a reply from another host, an HTML page and a reply that is not a JSON object are all
  read failures. Give the final, canonical portal URL.
- **Token in the query string.** Reads send the token as a query parameter, the form Esri's
  Common parameters page shows. A portal or proxy access log can record it, and the tool has no
  way to send it as a header. Writes send it in the POST body. Redirects are refused, so the
  token goes only to the host you named. The tool replaces the exact token, and its percent-encoded
  form, with `***` in any error text. A message that echoes only part of the token is not caught.
- **System proxy.** For a real portal the tool uses the proxy settings of the system. Loopback
  addresses never use one.
- **Console output.** A character that the console cannot encode prints as `?`. The text on disk
  is not changed.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [deadwidget](https://github.com/uhsear/deadwidget) - finds the widgets in an Experience Builder
  app that are bound to a layer that no longer exists. Run it to find the edit, and this tool to
  make it.
- [itemvault](https://github.com/uhsear/itemvault) - snapshots item configuration before you
  change it
- [whobreaks](https://github.com/uhsear/whobreaks) - lists what refers to an item before you
  change it
- [safe-republish](https://github.com/uhsear/safe-republish) - the same refusal before a
  destructive write, for a feature class
