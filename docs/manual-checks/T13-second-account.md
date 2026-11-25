# T13 manual check: machine-wide install and a second Windows account

Status: **done 2026-11-25** (run by the user; results below as the user reported them).

This check needs two Windows accounts on one computer: account A (your usual one) and account B (a second local or Microsoft account). It needs an administrator for the install. Do not run the installer from an account without admin rights.

Time: about 20 to 25 minutes, most of it in the two sign-ins and the model reply.

Shortcuts:
- If `dist\installer\local-agent-0.1.0-setup.exe` already exists and is current, skip the build (step 1).
- If the account-B chat (4.3) passes, the account-A chat (3.3) can be skipped; write "skipped (4.3 passed)" in the checklist.

The installer is **unsigned**, so Windows SmartScreen will warn when you run it. Click **More info**, then **Run anyway**.

Notes for the run:
- Ollama must be reachable from both accounts, as described in the README ("Multiple Windows accounts").
- Steps marked **admin** need an elevated prompt or the UAC prompt.
- Close all local-agent windows before installing and before uninstalling. The installer cannot close a console app, so a running `local-agent.exe` can block the file changes.
- Keep a note of anything unexpected, with the exact text.

## 1. Build the installer (account A)

From the repo root in PowerShell 7 or Windows PowerShell:

```powershell
packaging\build-installer.ps1
```

Expected: the last line is `Built ...\dist\installer\local-agent-0.1.0-setup.exe (about 22 MB)`. If the script warns that the exe may be stale, run it again with `-Rebuild`. If it says Inno Setup was not found, install it (`winget install --id JRSoftware.InnoSetup -e`) and run again.

## 2. Install as administrator (account A)

0. Before installing, save the raw machine PATH and its registry type. This reads the value without expanding `%VAR%` entries, which `(Get-ItemProperty ...).Path` does not show:

   ```powershell
   $k=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\CurrentControlSet\Control\Session Manager\Environment'); $k.GetValueKind('Path'); $k.GetValue('Path','','DoNotExpandEnvironmentNames') | Set-Content $env:USERPROFILE\Desktop\path-before.txt
   ```

   Write the kind (for example `String` or `ExpandString`) in the notes. The raw value is in `path-before.txt` on the Desktop. Delete that file when the check is done.

1. Right-click `dist\installer\local-agent-0.1.0-setup.exe` and choose **Run as administrator** (**admin**). Accept the UAC prompt. Accept the SmartScreen warning (**More info**, then **Run anyway**).
2. Click through the installer. Install to the default folder (`C:\Program Files\local-agent`). There is no Start-menu step.
3. Check: `C:\Program Files\local-agent\local-agent.exe` exists.
4. Check the machine PATH (read only):

   ```powershell
   (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment').Path -split ';' | Select-String 'local-agent'
   ```

   Expected: one line, `C:\Program Files\local-agent`.
5. Check the raw PATH (same registry read as step 2.0, without expanding names). Compare it with `path-before.txt`:

   ```powershell
   $k=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\CurrentControlSet\Control\Session Manager\Environment'); $k.GetValueKind('Path'); $k.GetValue('Path','','DoNotExpandEnvironmentNames')
   ```

   Expected: the value contains `C:\Program Files\local-agent` once, and every entry from the saved value is still there, unchanged, including any `%...%` text. The kind may now be `ExpandString`; that is expected.

## 3. Use it from account A

1. Close all terminals (including Windows Terminal and VS Code), then start a fresh one from the Start menu. Or sign out and back in. A terminal that was already open does not see the PATH change.
2. Run:

   ```powershell
   where.exe local-agent
   local-agent --help
   ```

   Expected: `where.exe` prints `C:\Program Files\local-agent\local-agent.exe`. `--help` prints the usage and exits 0.
3. Short chat. Ollama must be running and have the model from the README (`qwen3-vl:8b` by default; pass `--model` for another one):

   ```powershell
   cd $env:USERPROFILE\Documents
   local-agent
   ```

   At the prompt, type `Say hello in five words.` and check that a reply streams. Then type `exit`. (Skippable, see the shortcuts.)
4. Check that account A's state is in its own profile:

   ```powershell
   Get-ChildItem "$env:APPDATA\local-agent" -ErrorAction SilentlyContinue
   ```

   Expected: the folder may be missing (the app does not create config). If it exists, note what is in it.

## 4. Second account B

1. Sign in to account B (switch user; do not sign out of A if you want to come back quickly).
2. Close any terminal that was open in B before the install, then start a fresh one from the Start menu. Run:

   ```powershell
   where.exe local-agent
   local-agent --help
   ```

   Expected: the same path as in step 3, and `--help` exits 0 without any install step in B.
3. Check whether Ollama answers from account B, without starting anything. **Do not start Ollama in B.** Use a server that account A started:

   ```powershell
   curl.exe http://127.0.0.1:11434/api/tags
   ```

   Expected: JSON that lists the installed models.

   If that fails with a connection error and account A is not signed in (so its Ollama server is not running), either start Ollama from B (system tray or `ollama serve`), then run the same `curl.exe` again and record that B started the server, or skip step 4.3 and write "skipped: no Ollama server" in the checklist.

   Then, in B, run `local-agent` with the default host (it uses `127.0.0.1:11434` unless `OLLAMA_HOST` or `host` is set). At the prompt, type `Say hello in five words.` and check that a reply streams. Then type `exit`. If it does not, note the message and the address used (see the README, "Ollama and several accounts").
4. Check the state folder is separate:

   ```powershell
   Get-ChildItem "$env:APPDATA\local-agent" -ErrorAction SilentlyContinue
   echo $env:APPDATA
   ```

   Expected: `$env:APPDATA` is under B's profile (`C:\Users\<B>\AppData\Roaming`), not A's. Optional: in B only, create `%APPDATA%\local-agent\config.toml` containing the line `model = "qwen3-vl:8b"`, start `local-agent` in B and confirm A's folder (`C:\Users\<A>\AppData\Roaming\local-agent`) is unchanged.

## 5. Uninstall (account A, admin)

1. Close all local-agent windows. Then **Settings > Apps > Installed apps**, find local-agent, choose **Uninstall** (**admin**) and accept the UAC prompt.
2. Check the folder is gone:

   ```powershell
   Test-Path "C:\Program Files\local-agent"
   ```

   Expected: `False`.
3. Sign out of account A and back in (or reboot). The uninstall's PATH change is not documented to reach open sessions, so do this before the check. Then open a fresh terminal and check the PATH no longer has the entry:

   ```powershell
   (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment').Path -split ';' | Select-String 'local-agent'
   where.exe local-agent
   ```

   Expected: no output from `Select-String`, and `where.exe` reports `INFO: Could not find files for the given pattern(s).`
4. Check the raw PATH against the value saved in step 2.0:

   ```powershell
   $k=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\CurrentControlSet\Control\Session Manager\Environment'); $k.GetValueKind('Path'); $now=$k.GetValue('Path','','DoNotExpandEnvironmentNames'); $now -eq (Get-Content $env:USERPROFILE\Desktop\path-before.txt -Raw).TrimEnd([char]13,[char]10)
   ```

   Expected: `True`, so the raw string is exactly the one saved before the install. The kind may now be `ExpandString` instead of `String`. That is harmless: the installer writes the value as `REG_EXPAND_SZ`, and the text is the same. Note the kind in the checklist.
5. Check that account A's and B's `%APPDATA%\local-agent` folders were not deleted by the uninstall (the uninstaller does not remove them; that is expected).

## Results checklist

Fill in each line with `[x]` (pass), `[ ]` (not done) or `FAIL` with a note.

- [x] 1. Build prints `Built ...local-agent-0.1.0-setup.exe` (or the existing exe was used, noted).
- [ ] 2.0 Raw machine PATH saved before the install: not done (the installer was run before this step).
- [x] 2.1 The installer runs elevated and installs to `C:\Program Files\local-agent`.
- [x] 2.3 `local-agent.exe` exists in that folder.
- [x] 2.4 The machine PATH contains `C:\Program Files\local-agent` exactly once.
- [ ] 2.5 Raw (unexpanded) PATH comparison: not done; the user confirmed the entry was added.
- [x] 3.2 Account A, new terminal: `where.exe local-agent` shows the install folder; `local-agent --help` exits 0.
- [x] 3.3 Account A chat: `qwen2.5-coder:7b` wrote a prime-number function. (`qwen3-vl:8b` gave no reply; the machine was under heavy load.)
- [x] 4.2 Account B, new terminal: `where.exe local-agent` shows the same folder; `--help` exits 0.
- [x] 4.3 Account B (`gratio`): `local-agent` ran and got a model reply from account A's Ollama on the default host. `ollama` itself is not on B's PATH (the Ollama installer adds it per user); not needed by local-agent.
- [ ] 4.4 (not reported) `$env:APPDATA` differs between A and B, and B's state is not in A's folder.
- [x] 5.2 After uninstall, `C:\Program Files\local-agent` is gone.
- [x] 5.3 After sign-out/in or reboot, no PATH entry for local-agent; `where.exe local-agent` finds nothing.
- [ ] 5.4 Raw PATH equality: not checked (no saved raw value); the user confirmed the entry was removed.

Notes (what you saw, error text, the Ollama address used, Windows version):

```
```
