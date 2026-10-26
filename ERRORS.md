# ERRORS.md — recurring stumbles in this repo (check before similar tasks)

## 2026-10-26 — Multi-line code patches from the shell
**What didn't work:** Python patch scripts passed through a Bash heredoc (`cat > file <<'EOF'` / `python - <<'EOF'`) that contain nested quotes, `\\` line continuations or `'''` blocks: "unexpected EOF while looking for matching `''", or `assert a in s` failing on text that is really there. Git Bash `sed -i` also turns CRLF files into LF.
**What worked:** write the patch script into the scratchpad with the Write tool, then run it with `.venv/Scripts/python <script>`; or use the Edit tool directly for 1–3 replacements. Read/write with `newline=""` and keep the file's own line endings.
**Note for next time:** reserve heredocs for small, quote-free snippets. Tests need `.venv/Scripts/python` (system Python lacks itsdangerous); Playwright screenshots need the system `python` (Playwright is not in the venv).
