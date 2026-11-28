---
name: reverse-engineer
description: Reverse engineer shipped software with plain command-line tools - Electron/JS bundles, ELF/PE/Mach-O, Go/Rust/.NET/WASM, APKs, SQLite, pcaps, firmware - to explain a feature, find versions or dependencies, or compare builds.
tools: run_cmd, read_file, search, set_todos
---

# Reverse engineer: fast path

You already know these tools. This skill lists the fastest command for each kind of target and the traps.

## Rules

- Work on a copy. Put each extraction in its own folder under `.arena-artifacts/re/<name>` in this project, and reuse one that already exists.
- Route by `file -b T` and the magic bytes first, then run the cheapest inventory, and only then decompile or read code.
- `rg` skips hidden and ignored files, and bundlers hide code (Electron: `.webpack/`, `.vite/`). Always search extracted trees with `rg -uu`.
- Never read a big or minified file whole. Slice it: `rg -o '.{0,200}NEEDLE.{0,200}' F | head`. Dump a disassembly once to a file, then search that file.
- Batch independent probes into ONE `run_cmd` with `;` and `echo '--- name'` markers. Cap every output with `head` or `-m`.
- Answer from evidence. Mark each claim **seen** (cite file and offset, or a symbol or string) or **inferred**. A fact you did not find is **unknown**, not absent. Reading code is not watching it run.
- A missing tool is not a blocker: use the fallback in the same row. Install packages only if the user agrees (`sudo pacman -S`, `apt install`, `npm i -g`).
- Turns cost more than bytes. Aim for 2-4 tool calls: one batched inventory, one or two targeted slices, then the answer. Do not re-verify what you already saw.

## First move: one call each

Native ELF (Linux):
```sh
T=./bin; D=.arena-artifacts/re/$(basename $T); mkdir -p $D; file $T; echo '--- LIBS'; readelf -dW $T | rg NEEDED; echo '--- EXPORTS'; nm -D --defined-only $T | head -150; echo '--- IMPORTS'; nm -D --undefined-only $T | head -150; echo '--- STRINGS'; strings -a -n 6 $T | head -400; objdump -d --no-show-raw-insn -C $T > $D/dis.txt; echo '--- FUNCS'; rg -o '^[0-9a-f]+ <[^>]+>:' $D/dis.txt | head -150
```
Then read one function: `awk '/<FUNC_NAME>:$/{p=1;next} p&&/^[0-9a-f]+ <.*>:$/{exit} p' $D/dis.txt | rg 'call|j[a-z]+ |cmp|lea'`.

Electron app (`resources/app.asar`):
```sh
A=./resources/app.asar; X=.arena-artifacts/re/asar; rm -rf $X; npx -y @electron/asar extract $A $X && jq '{name,version,main}' $X/package.json; find $X -path '*/node_modules' -prune -o -type f \( -name '*.js' -o -name '*.node' \) -print | head -60; ls ${A}.unpacked/node_modules 2>/dev/null; rg -uu -l --glob '!node_modules' 'exposeInMainWorld' $X
```
Then slice the needle across the bundle: `rg -uu -o --glob '!node_modules' '.{0,250}NEEDLE.{0,250}' $X | head -20`.

Go: `go version -m T` alone usually answers the question.

## Router

| Target (how to tell) | Fastest path |
|---|---|
| Electron (`app.asar`) | `npx -y @electron/asar extract app.asar X` (also `app.asar.unpacked/` for `.node` addons). Entry: `jq .main X/package.json`. Surface: `rg -uu -n -o --glob '!node_modules' '.{0,120}(ipcMain\.(handle|on)|ipcRenderer\.(invoke|send)|contextBridge\.exposeInMainWorld|preload:|nodeIntegration|contextIsolation|sandbox).{0,160}' X`. Hidden IPC channel names: `rg -uu -o '"[a-z]+:[a-z0-9:-]+"' X/<main> \| sort -u`. Fuses: `npx -y @electron/fuses read --app <app dir>`. |
| Minified JS / web bundle | Find `sourceMappingURL`; a map with `sourcesContent` gives the sources (`jq -r '.sources[]'`). Webpack: search module ids, then slice around the hit. Pretty-print: `npx -y prettier --parser babel F > F.pretty.js`, then read line ranges. |
| Hermes / bytenode | Hermes magic `c6 1f bc 03` (`index.android.bundle`): `hermes-dec` / `hbc-decompiler`. `.jsc` (bytenode) cannot be decompiled: use strings and runtime hooks. |
| ELF (`file`: ELF) | `readelf -hlSW`, `readelf -dW` (NEEDED, RPATH), `nm -D --defined-only` / `--undefined-only` (imports = the capability map), `strings -a -n 6`, `objdump -d -M intel -C`. Hardening: `checksec --file=T` if installed. |
| PE (Windows `.exe`/`.dll`) | `objdump -x T \| head -200` (imports, sections), `strings -a -el T` for UTF-16, `wrestool -l T` for resources. |
| Mach-O (macOS binary) | On Linux: `llvm-objdump --macho -d T`, `llvm-nm -gU T`, `llvm-otool -L T` (from the llvm package). |
| Native code logic | Dump once: `objdump -d --no-show-raw-insn -C T > $D/dis.txt`. Find the function: `rg -n '<.*Name.*>:$'`. Read the call and string trail rather than every instruction. Real pseudocode only when needed: Ghidra `analyzeHeadless $D p -import T -postScript DecompileAll.java -deleteProject` or `r2 -qc 'aaa; pdc @ sym.X' T`. |
| Go (`.go.buildinfo`) | `go version -m T`: toolchain, module, deps, build flags. Without Go: `strings -a T \| rg -m1 -A200 'Go buildinf'`. Symbols survive stripping: `go tool nm T \| rg 'main\.'`. |
| Rust | Deps (cargo-auditable): `cargo audit bin T`, or extract the `.dep-v0` section and zlib-inflate it. Compiler: `strings -a T \| rg -o '/rustc/[0-9a-f]{40}' \| sort -u`. Crates: `strings -a T \| rg -o '[a-z0-9_-]+-[0-9]+\.[0-9]+\.[0-9]+/src' \| sort -u`. |
| .NET (`file`: Mono/.Net) | `ilspycmd -p -o X T.dll` (`dotnet tool install -g ilspycmd`), then `rg`. P/Invoke: `rg DllImport X`. NativeAOT is native: use the ELF/PE rows. |
| Android APK | `jadx -d X --show-bad-code a.apk`, then `rg`. Manifest and resources only: `apktool d --no-src -o X a.apk`. Quick look: `unzip -l a.apk`. `libflutter.so` means Flutter (Dart AOT: `blutter`); `libhermes.so` means React Native (Hermes row). |
| WASM | `wasm-objdump -x`, `wasm2wat`, `wasm-decompile` (wabt). |
| SQLite | Copy the db and its `-wal` file. `sqlite3 'file:C?mode=ro' '.schema'`, then `SELECT * FROM t LIMIT 20`. |
| pcap / HAR | `tshark -r C -q -z conv,tcp`; `tshark -r C -T fields -e dns.qry.name -e tls.handshake.extensions_server_name -e http.request.full_uri \| sort -u`. HAR: `jq -r '.log.entries[].request \| .method+" "+.url'`. |
| Firmware | `binwalk -e F` or `unblob -e X F`, then route each extracted binary through this table. |
| Website | `curl -sL URL` for the HTML and script sources, then fetch the JS and slice it. For behaviour, use the browser tools (`browser_open`, `browser_snapshot`) or `curl -sI` for headers. |
| Running process (Linux) | `strace -f -e trace=network,file -p PID`, `ltrace -p PID`, `lsof -p PID`, `ss -tpn`, `journalctl _PID=PID`. Electron main: `--inspect` only if its fuses allow it. Android: `adb logcat`, `frida -U -f pkg -l hook.js`. |

## Speed patterns

- Strings and imports first. On a small binary, imports + strings + exports usually answer "what does it do" with no disassembly.
- To find who uses a string: get its address (`rg -n -b 'X'` on the binary, or `objdump -s -j .rodata`), then search the disassembly for that address and take the enclosing function.
- A named call target (`getpeereid`, `connect`, `std::hash`) already tells you the protocol: read the call sequence, not every instruction.
- For JS, go from the native/JS boundary inward: the native call, its caller, the IPC registration, then the renderer or preload.
- Comparing versions: diff the inventories (`nm`, the module list, `go version -m`) before you diff any code.
- Rebuilding a feature: write the minimal reimplementation, then test it against the original with the same inputs and outputs.
