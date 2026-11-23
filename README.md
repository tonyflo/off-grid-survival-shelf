# Off-Grid Survival Shelf

**Build your own offline library of survival knowledge.**

Download practical information while you have an internet connection, then keep it on a computer, home server, or external drive:

- [Kiwix](https://kiwix.org/): offline encyclopedias, medical references, practical guides, and more.
- [The Last Light](https://bunkerbot.co.uk/download.html): an offline survival guide with reference books.
- [Survivor Library](https://www.survivorlibrary.com/): historical books organized by subject.

Start with a smaller library, add individual subjects, or download larger collections. These can take considerable time and storage.

## Get started

You'll need macOS or Linux, Python 3.8 or newer, and an internet connection for the download. If needed, install [Python 3](https://www.python.org/downloads/). With Git installed, run:

```bash
git clone https://github.com/tonyflo/off-grid-survival-shelf.git
cd off-grid-survival-shelf
bash setup.sh
```

You can also download the repository ZIP from GitHub, extract it, and open a terminal in the extracted folder before running `bash setup.sh`.

The **starter library** includes seven Kiwix references for health, emergency preparedness, practical skills, and recipes; five Survivor Library book collections covering canning, food, shelter, medical emergencies, and individual survival; and The Last Light guide. Choose the full starter or only the sources you want. The program shows available size estimates before asking whether to download.

To select only part of the Kiwix starter, add `medical`, `preparedness`, or `food` to `bash setup.sh starter kiwix`. These are groups chosen for this starter; Kiwix's own catalog categories are described below.

The setup menu also lets you browse more, download your saved selections, or check their status. Downloads go into the `offline-library/` folder in the project by default.

To use a different folder, edit `config.json`, which is created the first time you run a download command without a `CACHE_DATA` override. Set `library_path` to the folder you want, for example:

```json
{
  "library_path": "~/offline-library"
}
```

To choose your folder before the first run, copy `config.example.json` to `config.json` and edit it. Existing files are kept and matching downloads are skipped by filename; existing files are not checked for damage. If you're switching to a new checkout and want to use an existing library, set `library_path` to that library's folder.

If `CACHE_DATA` is already set in your terminal, it overrides the config file. Run `unset CACHE_DATA` to use `config.json` instead.

## Add more content

Kiwix content is discovered from its catalog. Survivor Library browsing uses a saved catalog, so it opens quickly. Use the setup menu to browse either one, or search for a topic:

```bash
bash setup.sh browse kiwix maps
bash setup.sh browse survivor food
```

Run `bash setup.sh browse kiwix` to choose one of Kiwix's own categories before browsing individual items. Press Enter at the category prompt to include all categories, then press Enter at the topic prompt to see all items in your choice. You can also search within a category, for example:

```bash
bash setup.sh browse kiwix --category wikipedia medicine
```

Some Kiwix entries have no category. Maps are also discovered from a separate index, so choose all categories to include those items. Category names come from Kiwix and may change. Results include a source link so you can look into them before choosing. The selected downloads are remembered for future runs. Searching Kiwix and downloading files require an internet connection.

Use the arrow keys to move, the space bar to select, and Enter to save your choices. Press `q` to cancel without saving. Choose whether to download your choices now. Run `bash setup.sh download` to download all missing saved selections. Kiwix content is in English.

If a Survivor category has no ZIP on its category page, its individual books
are downloaded instead. These categories can contain many separate files, and
their total size may be unavailable.

To update the Survivor catalog and check saved download sizes, run:

```bash
bash setup.sh update
```

This checks Survivor categories, the Kiwix starter and individually saved Kiwix selections, and Last Light. It saves the results in `offline-library/cache-in-case-catalog.json` without downloading library files. Checking every category can take several minutes; you can keep browsing while it runs. To refresh just one source, add `survivor`, `kiwix`, or `last-light` to the command.

To check a particular Survivor topic instead of the full catalog, for example:

```bash
bash setup.sh update survivor food
```

Sizes marked `~` come from the saved catalog or publisher and may have changed since they were checked. Other Kiwix results use sizes from Kiwix's catalog. The actual size is shown when a download starts, if the server provides it. `update` refreshes this information; `upgrade` checks for newer content.

To select the entire Survivor Library or a Kiwix category or full English catalog:

```bash
bash setup.sh survivor all
bash setup.sh kiwix all
```

The Kiwix command lists the publisher's categories: enter a category name to select its contents, or press Enter to select the full catalog, including uncategorized items and separately indexed maps. The selection is saved, so `download` can retry missing items and discover new items in that category. You can also specify a category directly, for example `bash setup.sh kiwix all wikipedia`.

Both commands show a size estimate before you choose. Kiwix uses its current catalog; Survivor uses saved category sizes and shows a partial estimate because some sizes and individual books are missing. Survivor categories can overlap, so some ZIPs may be counted more than once. Estimates do not subtract files you already have.

The complete catalogs can be extremely large. Check the available space before choosing either one. `survivor all` downloads the ZIPs and individual books it finds in the publisher's category and recent-books indexes, including books outside category ZIPs. It compares filenames to avoid downloading books already in a ZIP. The publisher may have older files that aren't linked from either index; those can't be discovered automatically.

## Preview, retry, and check progress

Add `--preview` to see what would happen without downloading or saving selections:

```bash
bash setup.sh --preview
bash setup.sh starter last-light --preview
bash setup.sh browse survivor food --preview
```

Once you have saved your selections, check on your library or retry missing downloads:

```bash
bash setup.sh status
bash setup.sh download
bash setup.sh update
bash setup.sh upgrade
```

If a download is interrupted, run `bash setup.sh download` again. Partial downloads can resume when the source supports it; completed files are skipped. If an existing file is empty, status reports it and download asks you to move it aside before retrying. When the publisher provides a download size, the program checks free space before transferring each file. In a terminal, progress updates on one line; the log keeps a record of progress.

Before downloading, the program lists missing files and estimated sizes. Some sizes are unavailable until the download starts, and the full collections may need further discovery. `status` shows saved sizes, estimated downloads remaining, and the size of your library folder, including extracted files.

New ZIPs are tested after downloading. Individual PDFs receive a basic file-type check. New Kiwix files are checked against the publisher's SHA-256 checksum when available; otherwise they receive a basic file-type check.

`upgrade` checks downloaded Kiwix content for newer editions and keeps your older files. If Last Light's package has changed, it is staged in `last-light/site-update/` for you to review; your current guide stays in `last-light/site/`. If the program can't tell whether it changed, it asks before downloading a copy. In the background, it skips that uncertain download. To stage a later package, move or remove the previous `site-update/` and `last-light-update.zip` first. Survivor ZIPs keep the same names, so newer versions cannot be detected. Run `download` to get missing Survivor files. Add `--preview` to see the plan without downloading.

For a long download, run your saved selections in the background:

```bash
nohup bash setup.sh download >/dev/null 2>&1 </dev/null &
```

Follow the log with:

```bash
bash setup.sh log
```

Press **Ctrl+C** to stop watching the log; the download continues.

To refresh the catalog in the background instead, run:

```bash
nohup bash setup.sh update >/dev/null 2>&1 </dev/null &
```

## Open your library offline

The files can be used directly, without a web server. Prepare and test your readers before disconnecting:

- Open `.zim` files in `offline-library/kiwix/` with [Kiwix Reader](https://get.kiwix.org/en/solutions/applications/kiwix-reader/).
- Open `offline-library/last-light/site/index.html` in a browser. Some features, including maps and offline AI, need a local server and additional setup.
- Extract the ZIPs in `offline-library/survivor-library/zips/` and open the books inside. Individual books downloaded separately are in `offline-library/survivor-library/books/`.

If you chose a different folder, look there instead of `offline-library/`.

## Access from a browser

For convenient access from the computer holding your library, start the optional Compose setup from the project folder after downloading:

If you chose a different folder in `config.json`, give Compose the same absolute path when starting it:

```bash
CACHE_DATA="$HOME/offline-library" docker compose -f web/compose.yaml up -d
```

For the default folder, run:

```bash
docker compose -f web/compose.yaml up -d
```

- [Kiwix](http://localhost:8089)
- [Last Light](http://localhost:8091)
- [Survivor Library](http://localhost:8092) (ZIPs and individual books)

The Survivor Library page lists ZIPs for download; extract them to read the books. Individual books can be opened directly. To serve just one collection, add `kiwix`, `last-light`, or `survivor` to the end of the command. After adding Kiwix files, run `docker compose -f web/compose.yaml restart kiwix` to load them. To stop the services, run `docker compose -f web/compose.yaml down`.

## Project and license

Off-Grid Survival Shelf is a personal project shared as-is, with no guarantee of ongoing updates or support. You're welcome to fork it.

The project code is available under the [MIT License](LICENSE). Downloaded books and guides have their own licenses and terms. The included catalog snapshot contains publisher titles, links, and sizes from Kiwix, The Last Light, and Survivor Library.
