import subprocess
import sys

def run_subprocess(command, timeout=None):
    """
    Runs a command in a subprocess, capturing output and handling platform-specific
    process creation flags to prevent the creation of new console windows on Windows.

    Args:
        command (list): The command to execute as a list of strings.
        timeout (int, optional): The timeout for the subprocess in seconds.

    Returns:
        A tuple containing the return code, stdout, and stderr.
    """
    if sys.platform == "win32":
        # startupinfo to prevent console pop-ups
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            startupinfo=startupinfo,
        )
    else:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        process.kill()
        # It's important to communicate again after killing to get any remaining output
        out, err = process.communicate()
        # Re-raise the exception with the captured output
        raise subprocess.TimeoutExpired(command, timeout, output=out, stderr=err) from e


    return process.returncode, stdout, stderr
