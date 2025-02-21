This script compares two remote folders on different servers via SSH/SFTP.
Features:
  • Recursively lists files (optionally follows symlinks with cycle detection).
  • Supports optional filtering via a compare list file.
  • Uses key-based or password-based authentication; if normal auth fails,
    falls back to interactive keyboard authentication.
  • For each file:
      - If its size is less than a threshold (default: 10 MB) and it is text,
        reads and diff’s line-by-line.
      - If larger than the threshold, or if binary, computes an MD5 hash (streaming way).
      - For DOCX and XLSX/XLSM files, attempts to extract text (if libraries installed).
      - For symlinks, if not following, returns the link target as text; if following,
        compares the target’s contents.
  • Calculates similarity percentage and outputs:
      - Two HTML reports (one for identical files, one for differences),
      - A separate HTML report listing all exception filters,
      - A text file listing different files,
      - Process and debug logs.
  • All output is stored in a timestamped (or user‑specified) folder.
  
Usage:
  python compare_remote_folders_html.py 
      <serverA> <userA> <keyA_or_None> <passA_or_None> <remote_folderA> 
      <serverB> <userB> <keyB_or_None> <passB_or_None> <remote_folderB> 
      [compare_list_file_or_None] [output_folder_or_None] [follow_symlinks (True/False)]
      [exception_folders_file_or_None] [exception_extensions_file_or_None] [date_threshold_or_None]
      [file_exceptions_file_or_None] [file_substring_exceptions_file_or_None]

Examples:
  1. Basic:
     python compare_remote_folders_html.py \
       192.168.1.10 alice /home/alice/.ssh/id_rsa None /var/www/html \
       203.0.113.5 bob None MyPassword /var/www/html
       
  2. With compare list, custom output folder, following symlinks, exception filters (folders, extensions, file names, substrings) and a date threshold:
     python compare_remote_folders_html.py \
       192.168.1.10 alice None MyPassword /home/alice/project \
       203.0.113.5 bob None MyPassword /home/bob/project \
       compare_list.txt MyOutput True exception_folders.txt exception_extensions.txt "2023-01-01 00:00:00" file_exceptions.txt file_substring_exceptions.txt
