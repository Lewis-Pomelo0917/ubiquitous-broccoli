The llama.cpp Windows CPU runtime uses the Visual C++ x64 runtime. This package carries the three app-local runtime files listed and SHA-pinned in `resource-lock.json`, sourced from the licensed Visual Studio VC145 x64 Redistributable directory. Redistribution is subject to the applicable Microsoft Software License Terms; builders must use a licensed Visual Studio installation and the redistributable files it provides.

The runtime DLLs are extracted beside the private llama.cpp worker under Cubic's app-local data directory, not beside the delivered EXE. The Universal CRT (`ucrtbase.dll` and Windows API-set DLLs) remains a Windows 10/11 operating-system component. WebView2 Runtime remains required by the Tauri desktop application.

Microsoft redistribution guidance: https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files
