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

This tool writes one edit to both surfaces and then proves it. It refuses before writing when a
surface does not hold the value you expected to replace. It refuses when the draft and the
published copy differ in structure, which means somebody has unpublished work in the builder. It
writes the draft first and the item data second. It then reads both surfaces again, with the
cache defeated, and exits non-zero unless every edited value reads back and nothing else moved.

```
$ python exbtwin.py --portal https://example.maps.arcgis.com --item <32 hex> --edits edits.json
  note: 1 text value(s) differ between the surfaces outside the edits; the builder rewrites markup, so this is not treated as drift
  draft: will change 1 edit(s)
  data: will change 1 edit(s)
VERDICT: ALLOW
Check only. Nothing was written. Re-run with --apply and --backup-dir to write: draft, then data.
```

## The existing alternative

[exb-publish-agol](https://github.com/teeks822828/exb-publish-agol) (MIT) is a Claude Code skill
that builds Experience Builder apps on ArcGIS Online by script, from cached donor configs. It
ships a validator for the config traps its author found, a pull and push helper that backs up
before a push, and a guard that refuses to write to anything but an ArcGIS Online organization.
If you want to author a new app, or add pages, charts and tables to one, use it. This tool does
not build anything.

The two do different jobs. This tool edits values that already exist, in an app that is already
published, and it checks the result. Its README, as read for this one, does not describe a
read-back of both surfaces, a check that the two surfaces agree before a write, or the order in
which the two are written. This tool was not tested against it, and the comparison stops there.

## What you need

Python 3.9 or newer, and no packages. The file parses under the 3.9 grammar, and the
self-test was run on 3.12 and 3.13 only. `--self-test` needs no portal, no token and no network beyond
a stub on `127.0.0.1`.

```
git clone https://github.com/uhsear/exbtwin.git
python exbtwin.py --self-test
```

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
environment variable. No flag takes the token itself, so it never appears in a process list.

| Flag | Default | What it does |
|---|---|---|
| `--portal` | none | Portal root, for example `https://example.maps.arcgis.com`. Must be https, except `localhost` and `127.0.0.1`. Required. |
| `--item` | none | The 32 character item id of the app. Required. |
| `--edits` | none | The edits file. Required. |
| `--token-file` | none | File holding the token. Falls back to `ARCGIS_TOKEN`. |
| `--backup-dir` | none | Folder that keeps `data.json`, `draft.json` and `inverse.json`. Required with `--apply`. |
| `--allow-divergent` | off | Write although the draft holds unpublished structural changes. |
| `--retries` | `3` | Read-backs before the verification gives up. |
| `--retry-delay` | `2.0` | Seconds between read-backs. |
| `--timeout` | `30` | Seconds per request. |
| `--apply` | off | Perform the write. Without it nothing is written, and no backup folder is made. |
| `--self-test` | off | Run the offline assertions and exit. |

The parser sets `allow_abbrev=False`. `--ap` does not mean `--apply`: it is a usage error
(exit 64), and the self-test pins that.

Exit codes: 0 done, or the check is clean. 1 refused. 2 a write failed. 3 written but not
verified. 4 the app could not be read. 64 usage error.

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
matter.

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
- either surface changed between the first read and the moment before the write.

A difference that is only a string is not drift. The builder rewrites markup when it saves, and
the report counts those strings in a note instead.

When it writes (`--apply`), it:

1. Saves the old item data, the old draft and `inverse.json` in `--backup-dir`. It refuses to
   overwrite a backup, so use an empty folder. `inverse.json` is an edits file. Run it with
   `--apply` to undo the edit.
2. Writes the draft with `updateResources`, then the item data with `update`.
3. Reads both surfaces again, each time with a throwaway `_ts` parameter and
   `Cache-Control: no-cache`. It checks that every edited path holds the new value, and that
   nothing else changed in keys, list lengths or non-text values. It retries up to `--retries`
   times, because the draft resource sits behind a CDN.
4. Exits 3 and prints the undo command if the read-back never matches.

If the item data write fails after the draft was written, the tool writes the old draft back and
reads it to confirm. It says when it could not confirm, and then the backup is the way back.

### Why the draft goes first

A crash between the two writes leaves one surface changed. Writing the draft first leaves a draft
that is ahead of the published copy. Visitors see the old app until the next Publish, which then
ships the fix. Writing the item data first leaves a published fix with an old draft, and the next
Publish removes the fix without a sign. The first state is the harmless one, so the tool
produces it. The self-test pins the order.

## The Esri behaviour it relies on

| Claim | Source | Exercised |
|---|---|---|
| The item data is the published copy and `config/config.json` is the draft. | [solution.js issue 660](https://github.com/Esri/solution.js/issues/660) | Documented, not exercised: the stub is built to this contract. |
| An app is an item of type `Web Experience` with a JSON data section. | [Items and item types](https://developers.arcgis.com/rest/users-groups-and-items/items-and-item-types/) | Stub: a `Web Map` item is refused. |
| `.../content/items/<id>/data` returns the JSON of the data section. | [Item Data](https://developers.arcgis.com/rest/users-groups-and-items/item-data/) | Stub, by every run. |
| `.../content/items/<id>/resources/<name>` returns a resource, and names may hold slashes. | [Item Resources](https://developers.arcgis.com/rest/users-groups-and-items/item-resources/) | Stub, by every run. |
| `update` takes `text` for the item's JSON and answers `{"success": ..., "id": ...}`. | [Update Item](https://developers.arcgis.com/rest/users-groups-and-items/update-item/) | Stub, by every applied run. |
| `updateResources` pairs `fileName` and `text` to change an existing text resource, `resourcesPrefix` must match, and it answers `success`. | [Update Resources](https://developers.arcgis.com/rest/users-groups-and-items/update-resources/) | Stub, by every applied run. |
| `token` and `f=json` are ordinary request parameters. | [Common parameters](https://developers.arcgis.com/rest/users-groups-and-items/common-parameters/) | Stub: the stub refuses a request without the right token. |
| The item record holds `owner`, and `ownerFolder` for the owner or an administrator. | [Item](https://developers.arcgis.com/rest/users-groups-and-items/item/) | Stub: a record with `ownerFolder` hidden fails the write cleanly. |

Three things the tool does that the pages above do not state:

- **The folder in the update path.** The current Update Item page shows
  `/content/users/<user>/items/<id>/update`. The tool inserts the item's `ownerFolder` when the
  record has one, because the User Content page lists a folder-scoped item resource. That is the least
  documented choice in the tool. Only the stub exercised it. If a portal rejects it, the write fails (exit 2) and the draft is restored. The read-back
  is the safety net.
- **The CDN cache on the draft.** No Esri page says that the resource is cached. The cache bust is
  there because a stale read after a write was seen in practice. The stub serves stale reads on
  demand and the self-test covers both a stale read that clears and one that does not.
- **The `Referer` header.** The tool sends the portal URL. Esri's Generate Token page ties a
  token to "the base URL of the client application that will use the token", and does not say
  that a header must match. The header is harmless when it is not needed.

## A real run

```
$ python exbtwin.py --self-test
exbtwin self-test: temporary files and a local stub only, no real portal, no credentials
--------------------------------------------------------------------
PASS  a pointer splits on slashes
...
PASS  an item id is exactly 32 hex characters
...
PASS  a unique prefix --ap of --apply is REFUSED by the parser  <-- pinned defect
...
PASS  the draft is written BEFORE the published copy  <-- pinned defect
...
PASS  a replacement written against one surface is REFUSED on the other, not silently skipped  <-- pinned defect
...
PASS  a write the portal acknowledges but did not store is caught  <-- pinned defect
...
PASS  a portal error that echoes the token is redacted before it is printed  <-- pinned defect
...
PASS  loading the file runs nothing by itself and exposes main
--------------------------------------------------------------------
149 assertions, 0 failed
```

The harness is the `check()` and `raises()` pair used across this portfolio. The test is part of
the tool: fixtures are synthetic, and the portal is a stub on `127.0.0.1` that listens for the
exact calls the tool makes. Among the cases pinned as defects:

- a prefix of the write flag (`--ap`) is refused by the parser;
- the draft is written before the published copy;
- a write the portal acknowledges but did not store (`success: true`, nothing changed) exits 3;
- a replacement written for one surface is refused on the other instead of silently skipped;
- a portal error that echoes the token is redacted before it is printed;
- the token reaches no output and no file. The test reads every file the run wrote and every
  line it printed, and looks for the token.

Branch coverage is measured with `coverage run --branch exbtwin.py --self-test`:

```
Name         Stmts   Miss Branch BrPart  Cover   Missing
--------------------------------------------------------
exbtwin.py    1030      0    252      0   100%
--------------------------------------------------------
```

All 149 assertions give the same output on Windows (Python 3.13) and on Linux (Python 3.12):
the two transcripts were compared line by line and are identical. Nothing in the test depends
on the platform. The one thing the offline run cannot reach is a real portal, see Limits.

## Limits

- **No live portal.** Every network path ran against a local stub, not against ArcGIS Online or
  ArcGIS Enterprise. The stub follows Esri's published pages. It cannot prove that a real portal
  accepts the folder form of the update path, honours the `_ts` cache bust or returns the
  resource byte for byte. The read-back exists for that reason, and the first real run should
  be a check run followed by a single edit on a copy of an app.
- **Whole document writes.** `update` and `updateResources` replace a whole surface. The tool
  reads the surfaces twice and compares them, but a save in the builder between the second read
  and the write is not detected. The same goes for an open builder tab: the builder holds its
  own copy and can save over the draft later. Close the builder, or tell the people who have it
  open, before you use `--apply`.
- **Replace only.** An edit changes a value that exists. It does not add a widget, a key or a
  list entry, and it does not delete one. Building an app is the other tool's job.
- **String drift is a note.** If somebody changes only a text value in the builder and does not
  publish, the tool counts it as markup noise and still writes. Structure and non-text values
  are checked.
- **The draft must exist.** An app that was never opened in the builder may have no
  `config/config.json`. The read fails (exit 4) and the tool does nothing.
- **An edit can still be wrong.** The tool checks that your value arrived on both surfaces. It
  does not check that the app works. Open the app, and read the `config` for anything that
  matters.
- **Token in the query string.** Reads send the token as a query parameter, the form Esri's
  Common parameters page shows. Writes send it in the POST body. Redirects are refused, so the
  token goes only to the host you named, and it is replaced by `***` in any error text.

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
