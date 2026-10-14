"""Double-click / `python run_wizard.py` opens the BTP IS Cutover Wizard window."""
import sys

from btp_is_cutover.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
