"""PowerShell stderr over SSH comes back as CLIXML; show only the message."""
from src.machines import clean_powershell_error

MSG = ("Another Odysseus install is already running on this computer. "
       "Wait for it to finish, then run this again if needed.")

# As the user saw it (the head of the block was cut off by a tail slice).
SAMPLE = (
    'bj><S S="Error">' + MSG + '_x000D__x000A_</S>'
    '<S S="Error">At line:31 char:73_x000D__x000A_</S>'
    '<S S="Error">+ ... CategoryInfo          : OperationStopped: (:) [], RuntimeException_x000D__x000A_</S>'
    '<S S="Error">+ FullyQualifiedErrorId : Another..._x000D__x000A_</S></Objs>')


def test_truncated_clixml_sample():
    assert clean_powershell_error(SAMPLE) == MSG


def test_full_clixml_block():
    raw = ('#< CLIXML\r\n<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
           '<S S="Error">Tom &amp; Jerry failed_x000D__x000A_</S>'
           '<S S="Error">At line:1 char:1_x000D__x000A_</S></Objs>')
    assert clean_powershell_error(raw) == "Tom & Jerry failed"


def test_plain_stderr_unchanged():
    assert clean_powershell_error("  ssh: connect to host x port 22: Connection refused\n") == \
        "ssh: connect to host x port 22: Connection refused"
    assert clean_powershell_error("") == ""
