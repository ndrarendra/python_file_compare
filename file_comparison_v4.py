"""
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
"""

import os
import sys
import stat
import difflib
import logging
import posixpath    # For remote (POSIX) path manipulations
import hashlib
import io
from datetime import datetime
import getpass

import paramiko
from paramiko.ssh_exception import AuthenticationException

# Optional libraries for DOCX and Excel text extraction.
try:
    from docx import Document
except ImportError:
    Document = None
    print("Warning: python-docx is not installed. DOCX files will be treated as binary.")
try:
    import openpyxl
except ImportError:
    openpyxl = None
    print("Warning: openpyxl is not installed. Excel files will be treated as binary.")

# Define a threshold (in bytes) for what we consider a "large" file.
LARGE_FILE_THRESHOLD = 10 * 1024 * 1024  # 10 MB

##############################################################################
# Logging Setup (handlers will be configured after output folder is determined)
##############################################################################
logger = logging.getLogger("remote_diff")
logger.setLevel(logging.DEBUG)

##############################################################################
# Keyboard-Interactive Handler for Multi-Factor Authentication
##############################################################################
def interactive_handler(title, instructions, prompt_list):
    responses = []
    print(title)
    print(instructions)
    for prompt, show_input in prompt_list:
        if show_input:
            responses.append(getpass.getpass(prompt))
        else:
            responses.append(input(prompt))
    return responses

##############################################################################
# SSH/SFTP Connection with Enhanced Authentication Fallback
##############################################################################
def connect_ssh(server, username, key, password):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if key in ["None", ""]:
        key = None
    if password in ["None", ""]:
        password = None
    try:
        if key and password:
            ssh.connect(server, username=username, key_filename=key, passphrase=password)
        elif key:
            ssh.connect(server, username=username, key_filename=key)
        elif password:
            ssh.connect(server, username=username, password=password)
        else:
            ssh.connect(server, username=username)
    except AuthenticationException:
        logger.warning(f"Standard authentication failed for {server}. Trying keyboard-interactive.")
        try:
            transport = paramiko.Transport((server, 22))
            transport.start_client()
            transport.auth_interactive(username, interactive_handler)
            ssh._transport = transport
        except Exception as ie:
            logger.error(f"Keyboard-interactive authentication failed for {server}: {ie}")
            raise
    except Exception as e:
        logger.error(f"Error connecting to {server} as {username}: {e}")
        raise
    return ssh

##############################################################################
# Recursive Remote File Listing with Optional Symlink Following
##############################################################################
def list_remote_files(sftp, remote_dir, follow_symlinks=False):
    file_dict = {}
    visited = set()  # To detect cycles when following symlinks
    try:
        items = sftp.listdir_attr(remote_dir)
        logger.info(f"Successfully listed: {remote_dir}")
        logger.debug(f"Directory listing for {remote_dir}: {[item.filename for item in items]}")
    except Exception as e:
        logger.error(f"Error listing directory {remote_dir}: {e}")
        return file_dict

    def recursive_list(current_path, base):
        try:
            for attr in sftp.listdir_attr(current_path):
                full_path = posixpath.join(current_path, attr.filename)
                logger.debug(f"Found: {full_path}")
                if stat.S_ISLNK(attr.st_mode):
                    try:
                        link_target = sftp.readlink(full_path)
                        logger.debug(f"Symlink: {full_path} -> {link_target}")
                        if follow_symlinks:
                            if not link_target.startswith('/'):
                                link_target = posixpath.join(posixpath.dirname(full_path), link_target)
                            if link_target in visited:
                                logger.warning(f"Cycle detected for symlink {full_path}, skipping follow.")
                                continue
                            visited.add(link_target)
                            try:
                                target_attr = sftp.stat(link_target)
                                if stat.S_ISDIR(target_attr.st_mode):
                                    recursive_list(link_target, base)
                                else:
                                    rel_path = posixpath.relpath(full_path, base)
                                    file_dict[rel_path] = ("file", link_target)
                            except Exception as e:
                                logger.error(f"Error stating symlink target {link_target}: {e}")
                                rel_path = posixpath.relpath(full_path, base)
                                file_dict[rel_path] = ("symlink", full_path, link_target)
                        else:
                            rel_path = posixpath.relpath(full_path, base)
                            file_dict[rel_path] = ("symlink", full_path, link_target)
                    except Exception as e:
                        logger.error(f"Error reading symlink {full_path}: {e}")
                    continue
                elif stat.S_ISDIR(attr.st_mode):
                    recursive_list(full_path, base)
                else:
                    rel_path = posixpath.relpath(full_path, base)
                    file_dict[rel_path] = ("file", full_path)
        except Exception as e:
            logger.error(f"Error accessing {current_path}: {e}")
    recursive_list(remote_dir, remote_dir)
    return file_dict

##############################################################################
# File Content Retrieval with Large File Streaming
##############################################################################
def get_file_content(sftp, remote_path_info):
    if isinstance(remote_path_info, tuple) and remote_path_info[0] == "symlink":
        link_target = remote_path_info[2]
        logger.debug(f"Processing symlink {remote_path_info[1]} with target: {link_target}")
        return (False, [f"SYMLINK: {link_target}"])
    if isinstance(remote_path_info, tuple):
        full_path = remote_path_info[1]
    else:
        full_path = remote_path_info
    try:
        file_stat = sftp.stat(full_path)
        file_size = file_stat.st_size
    except Exception as e:
        logger.error(f"Error stating file {full_path}: {e}")
        return (None, None)
    if file_size > LARGE_FILE_THRESHOLD:
        logger.info(f"File {full_path} is large ({file_size} bytes); using streaming MD5 comparison.")
        md5 = hashlib.md5()
        try:
            with sftp.open(full_path, 'rb') as f:
                while True:
                    chunk = f.read(4096)
                    if not chunk:
                        break
                    md5.update(chunk)
            return (True, md5.hexdigest())
        except Exception as e:
            logger.error(f"Error reading large file {full_path}: {e}")
            return (None, None)
    try:
        with sftp.open(full_path, 'rb') as remote_file:
            data = remote_file.read()
    except Exception as e:
        logger.error(f"Error reading file {full_path}: {e}")
        return (None, None)
    ext = posixpath.splitext(full_path)[1].lower()
    if ext == '.docx' and Document is not None:
        try:
            bio = io.BytesIO(data)
            doc = Document(bio)
            text = "\n".join(p.text for p in doc.paragraphs)
            logger.debug(f"Extracted text from DOCX file {full_path}.")
            return (False, text.splitlines())
        except Exception as e:
            logger.error(f"Error processing DOCX file {full_path}: {e}")
            return (True, data)
    elif ext in ['.xlsx', '.xlsm'] and openpyxl is not None:
        try:
            bio = io.BytesIO(data)
            wb = openpyxl.load_workbook(bio, read_only=True, data_only=True)
            texts = []
            for sheet in wb.worksheets:
                texts.append(f"Sheet: {sheet.title}")
                for row in sheet.iter_rows(values_only=True):
                    row_text = "\t".join(str(cell) if cell is not None else "" for cell in row)
                    texts.append(row_text)
            text = "\n".join(texts)
            logger.debug(f"Extracted text from Excel file {full_path}.")
            return (False, text.splitlines())
        except Exception as e:
            logger.error(f"Error processing Excel file {full_path}: {e}")
            return (True, data)
    elif ext in ['.doc', '.xls']:
        logger.warning(f"File {full_path} is an older Office format; treating as binary.")
        return (True, data)
    if b'\0' in data:
        logger.debug(f"File {full_path} detected as binary (null byte found).")
        return (True, data)
    else:
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError as e:
            logger.warning(f"UTF-8 decode error for {full_path}: {e}. Falling back to latin-1.")
            text = data.decode('latin-1')
        return (False, text.splitlines())

##############################################################################
# HTML Report Generation for Exceptions (Separate Report)
##############################################################################
def generate_exception_report(exceptions_info, output_filename, report_title="Exception Report"):
    html = f"""<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8">
    <title>{report_title}</title>
    <style>
      body {{
        font-family: Arial, sans-serif;
        margin: 20px;
        background-color: #f8f8f8;
        line-height: 1.5;
      }}
      h1 {{
        color: #333333;
      }}
      h2 {{
        color: #444444;
        border-bottom: 1px solid #cccccc;
        padding-bottom: 5px;
      }}
      ul {{
        list-style-type: disc;
        margin-left: 20px;
      }}
      .container {{
        background-color: #ffffff;
        padding: 20px;
        box-shadow: 0px 0px 10px #cccccc;
        margin-bottom: 20px;
      }}
    </style>
  </head>
  <body>
    <div class="container">
      <h1>{report_title}</h1>
"""
    # Loop over each type of exception and add them.
    for key, items in exceptions_info.items():
        html += f"<h2>{key}</h2>\n<ul>\n"
        if items:
            for item in sorted(items):
                html += f"<li>{item}</li>\n"
        else:
            html += "<li>None</li>\n"
        html += "</ul>\n"
    html += "</div>\n</body></html>"
    try:
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"Exception report generated: {output_filename}")
    except Exception as e:
        logger.error(f"Error writing exception report to {output_filename}: {e}")

##############################################################################
# HTML Report Generation for File Comparisons
##############################################################################
def generate_html_report(results, output_filename, report_title):
    html = f"""<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8">
    <title>{report_title}</title>
    <style>
      body {{
        font-family: Arial, sans-serif;
        margin: 20px;
        background-color: #f8f8f8;
        line-height: 1.5;
      }}
      h1 {{
        color: #333333;
      }}
      h2 {{
        color: #444444;
        border-bottom: 1px solid #cccccc;
        padding-bottom: 5px;
      }}
      ul {{
        list-style-type: none;
        padding-left: 0;
      }}
      li {{
        margin-bottom: 5px;
      }}
      .diff-table {{
        margin: 10px 0;
        border-collapse: collapse;
        width: 100%;
        table-layout: fixed;
      }}
      .diff-table td, .diff-table th {{
        border: 1px solid #ccc;
        padding: 4px;
        word-break: break-word;
        white-space: pre-wrap;
        overflow-wrap: break-word;
      }}
      .diff_header {{
        background-color: #eaeaea;
        font-weight: bold;
      }}
      .diff_add {{
        background-color: #d4fcbc;
      }}
      .diff_sub {{
        background-color: #fbb6c2;
      }}
      .diff_chg {{
        background-color: #ffffcc;
      }}
      a {{
        text-decoration: none;
        color: #0066cc;
      }}
      a:hover {{
        text-decoration: underline;
      }}
      .container {{
        background-color: #ffffff;
        padding: 20px;
        box-shadow: 0px 0px 10px #cccccc;
        margin-bottom: 20px;
      }}
    </style>
  </head>
  <body>
    <div class="container">
      <h1>{report_title}</h1>
      <h2>Table of Contents</h2>
      <ul>
"""
    for item in results:
        html += f'<li><a href="#{item["id"]}">{item["title"]}</a></li>\n'
    html += "</ul>\n</div>\n"
    for item in results:
        html += f'<div class="container"><h2 id="{item["id"]}">{item["title"]}</h2>\n'
        html += f'<p>{item["message"]}</p>\n'
        if "diff_table" in item:
            html += item["diff_table"] + "\n"
        html += "</div>\n"
    html += "</body></html>"
    try:
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"HTML report generated: {output_filename}")
    except Exception as e:
        logger.error(f"Error writing HTML report to {output_filename}: {e}")

##############################################################################
# Main Comparison Logic
##############################################################################
def main():
    if len(sys.argv) < 11:
        print("Not enough arguments.\n", main.__doc__)
        sys.exit(1)
    # Required arguments.
    serverA = sys.argv[1]
    userA = sys.argv[2]
    keyA = sys.argv[3]
    passA = sys.argv[4]
    folderA = sys.argv[5]
    serverB = sys.argv[6]
    userB = sys.argv[7]
    keyB = sys.argv[8]
    passB = sys.argv[9]
    folderB = sys.argv[10]
    # Optional arguments.
    compare_list_file = sys.argv[11] if len(sys.argv) >= 12 and sys.argv[11] not in ["None", ""] else None
    output_folder = sys.argv[12] if len(sys.argv) >= 13 and sys.argv[12] not in ["None", ""] else None
    follow_symlinks = sys.argv[13].lower() == "true" if len(sys.argv) >= 14 else False
    # Exception files.
    exception_folders = set()
    if len(sys.argv) >= 15 and sys.argv[14] not in ["None", ""]:
        try:
            with open(sys.argv[14], 'r', encoding='utf-8') as f:
                exception_folders = {line.strip() for line in f if line.strip()}
            logger.info(f"Loaded {len(exception_folders)} folder exceptions: {', '.join(sorted(exception_folders))}")
        except Exception as e:
            logger.error(f"Error reading exception folders file {sys.argv[14]}: {e}")
    exception_extensions = set()
    if len(sys.argv) >= 16 and sys.argv[15] not in ["None", ""]:
        try:
            with open(sys.argv[15], 'r', encoding='utf-8') as f:
                exception_extensions = {line.strip().lower() for line in f if line.strip()}
            logger.info(f"Loaded {len(exception_extensions)} extension exceptions: {', '.join(sorted(exception_extensions))}")
        except Exception as e:
            logger.error(f"Error reading exception extensions file {sys.argv[15]}: {e}")
    date_threshold_dt = None
    if len(sys.argv) >= 17 and sys.argv[16] not in ["None", ""]:
        try:
            date_threshold_dt = datetime.strptime(sys.argv[16], "%Y-%m-%d %H:%M:%S")
            logger.info(f"Using date threshold: {date_threshold_dt}")
        except Exception as e:
            logger.error(f"Error parsing date threshold {sys.argv[16]}: {e}")
    file_exceptions = set()
    if len(sys.argv) >= 18 and sys.argv[17] not in ["None", ""]:
        try:
            with open(sys.argv[17], 'r', encoding='utf-8') as f:
                file_exceptions = {line.strip() for line in f if line.strip()}
            logger.info(f"Loaded {len(file_exceptions)} file exceptions (exact match): {', '.join(sorted(file_exceptions))}")
        except Exception as e:
            logger.error(f"Error reading file exceptions file {sys.argv[17]}: {e}")
    file_substring_exceptions = set()
    if len(sys.argv) >= 19 and sys.argv[18] not in ["None", ""]:
        try:
            with open(sys.argv[18], 'r', encoding='utf-8') as f:
                file_substring_exceptions = {line.strip() for line in f if line.strip()}
            logger.info(f"Loaded {len(file_substring_exceptions)} file substring exceptions: {', '.join(sorted(file_substring_exceptions))}")
        except Exception as e:
            logger.error(f"Error reading file substring exceptions file {sys.argv[18]}: {e}")
    if not output_folder:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_folder = f"compare_output_{timestamp}"
    os.makedirs(output_folder, exist_ok=True)
    # Configure logging.
    logger.handlers.clear()
    process_log_path = os.path.join(output_folder, "process.log")
    debug_log_path = os.path.join(output_folder, "debug.log")
    formatter_obj = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    process_handler = logging.FileHandler(process_log_path, encoding='utf-8')
    process_handler.setLevel(logging.INFO)
    process_handler.setFormatter(formatter_obj)
    logger.addHandler(process_handler)
    debug_handler = logging.FileHandler(debug_log_path, encoding='utf-8')
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(formatter_obj)
    logger.addHandler(debug_handler)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter_obj)
    logger.addHandler(console_handler)
    logger.info("Starting folder comparison...")
    sshA = connect_ssh(serverA, userA, keyA, passA)
    sftpA = sshA.open_sftp()
    sshB = connect_ssh(serverB, userB, keyB, passB)
    sftpB = sshB.open_sftp()
    logger.info(f"Listing files in {folderA} on Server A")
    filesA = list_remote_files(sftpA, folderA, follow_symlinks=follow_symlinks)
    logger.info(f"Found {len(filesA)} files on Server A in {folderA}")
    logger.info(f"Listing files in {folderB} on Server B")
    filesB = list_remote_files(sftpB, folderB, follow_symlinks=follow_symlinks)
    logger.info(f"Found {len(filesB)} files on Server B in {folderB}")
    if compare_list_file:
        logger.info(f"Reading compare list from {compare_list_file}")
        try:
            with open(compare_list_file, 'r', encoding='utf-8') as f:
                wanted = {line.strip() for line in f if line.strip()}
            logger.info(f"Compare list contains {len(wanted)} items.")
            filesA = {k: v for k, v in filesA.items() if k in wanted}
            filesB = {k: v for k, v in filesB.items() if k in wanted}
        except Exception as e:
            logger.error(f"Error reading compare list file {compare_list_file}: {e}")
    all_files = set(filesA.keys()).union(set(filesB.keys()))
    logger.info(f"Total unique file paths to compare: {len(all_files)}")
    def is_exception_folder(rel_path):
        for folder in exception_folders:
            if rel_path.startswith(folder):
                return True
        return False
    results = []
    for rel_path in sorted(all_files):
        if is_exception_folder(rel_path):
            logger.info(f"Skipping {rel_path} due to folder exception.")
            continue
        ext = posixpath.splitext(rel_path)[1].lower()
        if ext in exception_extensions:
            logger.info(f"Skipping {rel_path} due to extension exception ({ext}).")
            continue
        base_name = posixpath.basename(rel_path)
        if base_name in file_exceptions:
            logger.info(f"Skipping {rel_path} because its file name '{base_name}' is in the file exceptions list.")
            continue
        if any(substr in base_name for substr in file_substring_exceptions):
            logger.info(f"Skipping {rel_path} because its file name '{base_name}' contains a disallowed substring.")
            continue
        if date_threshold_dt:
            skip_due_to_date = True
            file_a = filesA.get(rel_path)
            if file_a:
                try:
                    stat_a = sftpA.stat(file_a[1])
                    mtime_a = datetime.fromtimestamp(stat_a.st_mtime)
                    if mtime_a >= date_threshold_dt:
                        skip_due_to_date = False
                except Exception as e:
                    logger.error(f"Error getting modification time for {file_a[1]}: {e}")
            file_b = filesB.get(rel_path)
            if file_b:
                try:
                    stat_b = sftpB.stat(file_b[1])
                    mtime_b = datetime.fromtimestamp(stat_b.st_mtime)
                    if mtime_b >= date_threshold_dt:
                        skip_due_to_date = False
                except Exception as e:
                    logger.error(f"Error getting modification time for {file_b[1]}: {e}")
            if skip_due_to_date:
                logger.info(f"Skipping {rel_path} because modification time is before threshold.")
                continue
        result = {}
        file_id = rel_path.replace('/', '_').replace(' ', '_').replace('.', '_')
        result["id"] = file_id
        result["title"] = f"File: {rel_path}"
        file_a = filesA.get(rel_path)
        file_b = filesB.get(rel_path)
        if file_a and file_b:
            is_binary_a, contentA = get_file_content(sftpA, file_a)
            is_binary_b, contentB = get_file_content(sftpB, file_b)
            if is_binary_a is None or is_binary_b is None:
                result["message"] = "Error reading one of the files."
            elif is_binary_a != is_binary_b:
                result["message"] = "File type differs between servers (one binary, one text)."
            else:
                if is_binary_a:
                    hashA = contentA if isinstance(contentA, str) else hashlib.md5(contentA).hexdigest()
                    hashB = contentB if isinstance(contentB, str) else hashlib.md5(contentB).hexdigest()
                    if hashA == hashB:
                        result["message"] = "Binary file is identical."
                    else:
                        result["message"] = f"Binary file differs. MD5 Server A: {hashA}, Server B: {hashB}"
                else:
                    if contentA == contentB:
                        result["message"] = "The file is identical."
                    else:
                        result["message"] = "Differences found:"
                        diff_table = difflib.HtmlDiff().make_table(
                            contentA, contentB,
                            fromdesc=f"Server A: {rel_path}",
                            todesc=f"Server B: {rel_path}",
                            context=True, numlines=5
                        )
                        result["diff_table"] = diff_table
        elif file_a and not file_b:
            result["message"] = "File exists on Server A only."
        elif file_b and not file_a:
            result["message"] = "File exists on Server B only."
        results.append(result)
    sftpA.close()
    sshA.close()
    sftpB.close()
    sshB.close()
    identical_results = [r for r in results if r["message"].lower().startswith("the file is identical") or r["message"].lower().startswith("binary file is identical")]
    diff_results = [r for r in results if r not in identical_results]
    total_count = len(results)
    identical_count = len(identical_results)
    similarity_percent = (identical_count / total_count * 100.0) if total_count else 100.0
    logger.info(f"Similarity: {similarity_percent:.2f}% ({identical_count} out of {total_count} files are identical)")
    diff_list_path = os.path.join(output_folder, "diff_files.txt")
    with open(diff_list_path, "w", encoding="utf-8") as f:
        for r in diff_results:
            f.write(f"{r['title']} -> {r['message']}\n")
    logger.info(f"List of different files written to: {diff_list_path}")
    exceptions_info = {
        "Folder Exceptions": exception_folders,
        "Extension Exceptions": exception_extensions,
        "File Exceptions (Exact)": file_exceptions,
        "File Substring Exceptions": file_substring_exceptions
    }
    same_html = os.path.join(output_folder, "same_report.html")
    diff_html = os.path.join(output_folder, "diff_report.html")
    generate_html_report(identical_results, same_html, f"Identical Files Report (Similarity: {similarity_percent:.2f}%)")
    generate_html_report(diff_results, diff_html, f"Differences Report (Similarity: {similarity_percent:.2f}%)")
    # Generate a separate HTML report for exceptions.
    exceptions_report = os.path.join(output_folder, "exceptions_report.html")
    generate_exception_report(exceptions_info, exceptions_report, "Applied Exception Filters")
    logger.info("Folder comparison complete.")

if __name__ == "__main__":
    main()
