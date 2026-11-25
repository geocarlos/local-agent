"""Python mirror of the PATH edits in packaging/local-agent.iss ([Code] section).

The installer is Pascal, and no Inno Setup compiler runs in tests. These functions do the same steps
in the same order, so the rules can be tested here. Keep them in step with NormalizeDir, PathHasDir,
AddDirToPath and RemoveDirFromPath in the .iss file.
"""


def normalize(entry: str) -> str:
    """Trim spaces, drop trailing backslashes (trimming again each time), and lower-case."""
    text = entry.strip(" \t\r\n")
    while text.endswith("\\"):
        text = text[:-1].strip(" \t\r\n")
    return text.lower()


def path_has_dir(path_value: str, directory: str) -> bool:
    """True when one ';' entry of path_value is the same folder as directory. Empty entries never match."""
    target = normalize(directory)
    return any(normalize(part) != "" and normalize(part) == target for part in path_value.split(";"))


def add_dir(path_value: str, directory: str) -> str:
    """path_value with directory appended, or unchanged when the folder is already an entry."""
    if path_has_dir(path_value, directory):
        return path_value
    if path_value.strip(" \t\r\n") == "":
        return directory
    if path_value.endswith(";"):
        return path_value + directory
    return path_value + ";" + directory


def remove_dir(path_value: str, directory: str) -> str:
    """path_value without the entries for directory. Other entries, including empty ones, are kept as they are."""
    target = normalize(directory)
    kept = [part for part in path_value.split(";") if normalize(part) == "" or normalize(part) != target]
    return ";".join(kept)
