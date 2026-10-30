"""Every visible text / label the automation relies on, in one place.

If Bluestar (Dynamics 365) renames a button after an update, fix it here only.
All values were taken from the reference screenshots in
"Process 1 - Download CAD Files from Bluestar PLM".
"""

import re

# Stable internal control names (data-dyn-controlname) read from the live Bluestar page.
# These are tried first; the visible labels below are the fallback.
CTRL_CHECK_OUT = "cmdCheckOut"
CTRL_VIEW_FILES = "cmdViewFiles"
CTRL_FILE_INFO = "mibFiles"
CTRL_BOM_LINES = "mibBOMLines"
CTRL_OPEN_IN_OFFICE = "SystemDefinedOfficeButton"     # standard Dynamics 365 control name

# Engineering objects list page
FILTER_BOX_LABELS = ["Filter"]                       # quick filter text box
FILTER_FIELD_OPTION = re.compile(r"^\s*Object ID\s*:", re.I)   # "Object ID: "*22158-1000ME*""
REVISION_FILTER_LABEL = "Revision and approval filter"
REV_LATEST = "Latest revisions"
REV_ALL_APPROVED = "All approved revisions"
REV_ALL = "All"
ENGINEERING_TAB = "Engineering object"

GRID_HEADER_OBJECT_ID = "object id"
COL_OBJECT_ID = "object id"
COL_OBJECT_NAME = "object name"
COL_REVISION = "revision"
COL_STATUS = "status"

# Check out
BTN_CHECK_OUT = "Check out"
CHECKOUT_PANEL_TITLE = re.compile(r"^\s*Check out\s*:", re.I)
CAD_SYSTEM_TITLE = "Select CAD system"
APPROVED_WARNING = re.compile(r"has been approved and cannot be modified", re.I)
CLIENT_NOT_RUNNING = re.compile(r"Client is currently not running", re.I)
FORCE_FULL_CHECKOUT = "Force full check out"
CHECKOUT_PATH_HINT = re.compile(r"myloadpoint", re.I)  # default value of the "Checkout to" box

# Pop-ups while waiting (STEP-5.7)
BTN_WAIT = "Wait"                                    # "The operation is taking a long time ..."
BTN_IM_HERE = re.compile(r"^I.?m here$", re.I)       # "Are you still there?"
GENERIC_CONTINUE_BUTTONS = ["Yes", "OK", "Ok", "Continue", "Close"]
BUSY_TEXT = re.compile(r"Please wait\. We.re processing your request", re.I)

# File info / 2D draft (STEP-6)
BTN_FILE_INFO = "File info"
GRID_HEADER_FILE_TYPE = "file type"
COL_FILE_NAME = "file name"
COL_FILE_TYPE = "file type"
BTN_EXPORT_FILE = "Export file"
EXPORT_FOLDER_LABEL = "Select destination folder"
OPEN_FILE_QUESTION = re.compile(r"Do you want to open", re.I)
BTN_BACK = "Back"

# BOM (STEP-7)
BTN_BOM_LINES = "Lines"                              # first "Lines" button = BOM group
BOM_PAGE_TITLE = "BOM lines"
BTN_OPEN_IN_OFFICE = "Open in Microsoft Office"
MENU_EXPORT_BOM_LINES = "BOM lines"
BTN_DOWNLOAD = "Download"
