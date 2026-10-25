"""Exit 0 when an answer file lists C:\Drivers as a driver path in the specialize pass (checks-drivers.sh)."""

import sys
import xml.etree.ElementTree as ET

NS = {"u": "urn:schemas-microsoft-com:unattend"}
PATH = ("u:settings[@pass='specialize']/u:component[@name='Microsoft-Windows-PnpCustomizationsNonWinPE']"
        "/u:DriverPaths/u:PathAndCredentials/u:Path")

path = ET.parse(sys.argv[1]).getroot().find(PATH, NS)
sys.exit(0 if path is not None and path.text == "C:\Drivers" else 1)
