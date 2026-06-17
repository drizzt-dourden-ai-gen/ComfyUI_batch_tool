ComfyUI_batch_tool is a tool that can be used to run long generation runs by automaticly incramentng the seed for a specific number of images and changing to a different range automatically

Desktop UI for ComfyUI multi-workflow batch generation.
Detects workflow JSON files in the same folder, lets you configure
seed ranges, filename prefixes, enable/disable toggles, saves settings
to config.json, and runs generation with a live log display.

Requirements:
    pip install websocket-client requests
    tkinter is included with standard Python on Windows

Place this script in the same folder as your workflow JSON files.
Create a desktop shortcut pointing to:
    pythonw.exe comfy_batch_ui.py
(pythonw suppresses the terminal window)
