' xwatch をウィンドウなしで起動します（Windowsのスタートアップ登録用）
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
On Error Resume Next
sh.Run "pyw -3 """ & dir & "\xwatch.py"" --no-browser", 0, False
If Err.Number <> 0 Then
  Err.Clear
  sh.Run "pythonw """ & dir & "\xwatch.py"" --no-browser", 0, False
End If
