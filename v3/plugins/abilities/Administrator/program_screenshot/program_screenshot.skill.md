# Taking screenshots of programs

## When to use this
Call `program_screenshot` when the user asks you to screenshot, describe, or
"look at" what's showing in a program — e.g. "screenshot Chrome," "what's in
my Notepad?", "read the error in Visual Studio."

## How to use it

### Basic screenshot + full description
`program_screenshot("chrome")` — screenshots the Chrome window (starts Chrome
first if it isn't running) and returns a detailed text description of
everything visible.

### Focused question
`program_screenshot("notepad", "read the text in the file")` — screenshots
Notepad but asks the vision model only about the file contents, keeping the
answer tight.

### Program name matching
The `program_name` is the process name, case-insensitive. Common ones work
directly: `chrome`, `firefox`, `notepad`, `spotify`, `vscode`, `slack`,
`discord`, `teams`, `outlook`, `excel`, `word`, `powershell`, `cmd`,
`terminal`, `explorer`, `settings`.

## What it returns
- `status: ok` with a `description` field containing the full text description
- Plus the program name, window dimensions, and which vision model was used
- On error: `status: error` with a `message` explaining what went wrong

## What it does under the hood
1. Finds the program's window by process name (MainWindowHandle)
2. If not running, uses `Start-Process` to launch it and waits
3. Restores the window if minimised and brings it to the foreground
4. Captures the window rectangle with .NET Graphics.CopyFromScreen
5. Saves the PNG to the conversation as an attachment
6. Sends it to the configured vision model for description
7. Returns the description — you never see the raw PNG

## Tips
- Be specific with `question` when you only need one piece of info — it keeps
  the answer focused and faster
- If the first description misses a detail, call again with a tighter question
- The tool starts the program if it isn't running, but a newly launched program
  may show a loading screen — if the description seems wrong, wait a moment
  and call again
- Some UWP/modern apps (Calendar, Photos) use hosted process names — try
  the classic app name first
