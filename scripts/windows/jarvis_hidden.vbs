' Starts Jarvis without any visible window. Used by the "Jarvis" scheduled task (autostart.ps1).
' Child processes (Claude CLI, npx) inherit this hidden console, so nothing flashes on screen.
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName)))
python = root & "\.venv\Scripts\python.exe"
If Not fso.FileExists(python) Then
    MsgBox "Jarvis no encuentra " & python & vbCrLf & "Ejecuta scripts\windows\install.ps1", vbExclamation, "Jarvis"
    WScript.Quit 1
End If
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = root
shell.Run """" & python & """ -m jarvis service", 0, False
