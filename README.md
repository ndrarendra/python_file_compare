Remote Folder Comparison Tool with Flask Web Interface.


Features:
  • Recursively lists files on two remote servers via SSH/SFTP.
  • Compares file content (line-by-line diff for text files, streaming MD5 for large/binary files).
  • Supports DOCX and Excel text extraction (if libraries installed).
  • Exception filtering based on:
      - Folders,
      - File extensions,
      - Exact file names,
      - File name substrings.
  • Date threshold filtering (only files modified on/after a given date).
  • Parallel processing of file comparisons using ThreadPoolExecutor.
  • HTML reports with summary statistics, interactive filters, and a link to view the log.
  • A separate Exception Report that includes a timestamp and a table of all excluded files
    (with modification times from both servers).
  • Configuration via JSON (use null for “None”) or via an input form.
  • An interactive Flask web interface:
      - When a JSON file is uploaded, the individual form fields are hidden and their validation is skipped.
      - When the submit button is clicked, the form is visually “disabled” (via CSS) and a loading message is shown.
  • Dynamic file serving via a /download route (compatible with Linux and Windows).

Usage (CLI mode):
  python compare_remote_folders.py <serverA> <userA> <keyA_or_None> <passA_or_None> <remote_folderA>
                                  <serverB> <userB> <keyB_or_None> <passB_or_None> <remote_folderB>
                                  [compare_list_file_or_None] [output_folder_or_None] [follow_symlinks (True/False)]
                                  [exception_folders_file_or_None] [exception_extensions_file_or_None] [date_threshold_or_None]
                                  [file_exceptions_file_or_None] [file_substring_exceptions_file_or_None]
                                  [config_file_or_None]

Usage (Web mode):
  python compare_remote_folders.py --web
  (Then open http://0.0.0.0:5000 in your browser.)

Example JSON config (use null instead of "None"):
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