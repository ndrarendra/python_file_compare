# Remote Folder Comparison Tool

A tool for comparing folder structures and file contents between two remote servers over SSH/SFTP. It provides both a **CLI mode** and a **Flask-based web interface**.

## Features

- **Recursive file listing** on two remote servers via SSH/SFTP.
- **File content comparison**:
  - **Text files**: Line-by-line diff.
  - **Large/Binary files**: Streaming MD5 checksum.
  - **DOCX & Excel**: Text extraction (if libraries are installed).
- **Exception filtering** based on:
  - Folders
  - File extensions
  - Exact file names
  - File name substrings
- **Date threshold filtering** (only compares files modified on/after a given date).
- **Parallel processing** of file comparisons using `ThreadPoolExecutor`.
- **Generates HTML reports** with:
  - Summary statistics
  - Interactive filters
  - A downloadable log
- **Exception Report** includes:
  - Timestamp
  - List of excluded files (with modification times from both servers)
- **Configuration via JSON file** or web input form.
- **Flask Web Interface**:
  - Upload a JSON file to auto-fill the form.
  - Form validation is skipped when using JSON config.
  - Submit button visually “disables” the form with a loading message.
- **Supports dynamic file serving** via `/download` route (compatible with Linux & Windows).

---

## Installation

Ensure you have Python installed, then install the required dependencies:

```sh
pip install -r requirements.txt


python compare_remote_folders.py <serverA> <userA> <keyA_or_None> <passA_or_None> <remote_folderA> \
                                  <serverB> <userB> <keyB_or_None> <passB_or_None> <remote_folderB> \
                                  [compare_list_file_or_None] [output_folder_or_None] [follow_symlinks (True/False)] \
                                  [exception_folders_file_or_None] [exception_extensions_file_or_None] [date_threshold_or_None] \
                                  [file_exceptions_file_or_None] [file_substring_exceptions_file_or_None] \
                                  [config_file_or_None]

python compare_remote_folders.py 192.168.1.10 alice null null /var/www/html \
                                 203.0.113.5 bob null MyPassword /var/www/html \
                                 compare_list.txt MyOutput True \
                                 exception_folders.txt exception_extensions.txt "2023-01-01 00:00:00" \
                                 file_exceptions.txt file_substring_exceptions.txt config.json
```

Web Mode
Run the web interface:
```sh
python compare_remote_folders.py --web
```
Then, open http://0.0.0.0:5000 in your browser.

JSON Configuration
Instead of passing arguments in the CLI, you can use a JSON configuration file.
```sh
Example config.json:

{
  "serverA": "192.168.1.10",
  "userA": "alice",
  "keyA": null,
  "passA": null,
  "remote_folderA": "/var/www/html",
  "serverB": "203.0.113.5",
  "userB": "bob",
  "keyB": null,
  "passB": "MyPassword",
  "remote_folderB": "/var/www/html",
  "compare_list_file": "compare_list.txt",
  "output_folder": "MyOutput",
  "follow_symlinks": true,
  "exception_folders": "exception_folders.txt",
  "exception_extensions": "exception_extensions.txt",
  "date_threshold": "2023-01-01 00:00:00",
  "file_exceptions": "file_exceptions.txt",
  "file_substring_exceptions": "file_substring_exceptions.txt",
  "max_workers": 10
}

```

---

This `README.md` is **fully formatted and ready to use**! Let me know if you need any tweaks or additions. 🚀






