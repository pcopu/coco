"""Callback data constants for Telegram inline keyboards.

Defines all CB_* prefixes used for routing callback queries in the bot.
Each prefix identifies a specific action or navigation target.

Constants:
  - CB_HISTORY_*: History pagination
  - CB_DIR_*: Directory browser navigation
  - CB_MODEL_*: Model/reasoning selector menu
  - CB_UPDATE_*: Codex update panel actions
  - CB_APPROVAL_*: Session approval mode menu
  - CB_APP_APPROVAL_*: App-server interactive approval requests
  - CB_ALLOWED_*: Allowed-user manager menu
  - CB_APPS_*: Apps panel + looper configurator
  - CB_SESSION_*: Session lifecycle controls
  - CB_WORKTREE_*: Worktree manager actions
"""

# History pagination
CB_HISTORY_PREV = "hp:"  # history page older
CB_HISTORY_NEXT = "hn:"  # history page newer

# Directory browser
CB_DIR_SELECT = "db:sel:"
CB_DIR_MACHINE_SELECT = "db:mach:"  # db:mach:<machine_id>
CB_DIR_UP = "db:up"
CB_DIR_NEW_FOLDER = "db:new"
CB_DIR_CONFIRM = "db:confirm"
CB_DIR_CANCEL = "db:cancel"
CB_DIR_PAGE = "db:page:"
CB_DIR_SESSION_RESUME = "db:sres:"  # db:sres:<index>
CB_DIR_SESSION_FRESH = "db:sfresh"
CB_DIR_SESSION_BACK = "db:sback"
CB_DIR_SESSION_PAGE = "db:spage:"  # db:spage:<index>

# Model selector
CB_MODEL_SET = "md:set:"  # md:set:<model_slug>
CB_MODEL_EFFORT_SET = "md:eff:"  # md:eff:<effort>
CB_MODEL_REFRESH = "md:ref"

# Update panel
CB_UPDATE_REFRESH = "up:ref"
CB_UPDATE_RUN = "up:run"

# Session approvals
CB_APPROVAL_SET = "ap:set:"  # ap:set:<mode>
CB_APPROVAL_SET_DEFAULT = "ap:dset:"  # ap:dset:<mode>
CB_APPROVAL_REFRESH = "ap:ref"
CB_APPROVAL_REFRESH_DEFAULT = "ap:ref:def"
CB_APPROVAL_OPEN_DEFAULTS = "ap:view:def"
CB_APPROVAL_OPEN_WINDOW = "ap:view:win"

# App-server approvals
CB_APP_APPROVAL_DECIDE = "asa:dec:"  # asa:dec:<token>:<action>

# Allowed-user manager
CB_ALLOWED_ADD = "au:add"
CB_ALLOWED_ADD_SINGLE = "au:add:single"
CB_ALLOWED_ADD_CREATE = "au:add:create"
CB_ALLOWED_PICK_PAGE = "au:pick:pg:"  # au:pick:pg:<page>
CB_ALLOWED_PICK_TOGGLE = "au:pick:tg:"  # au:pick:tg:<user_id>
CB_ALLOWED_PICK_NEXT = "au:pick:next"
CB_ALLOWED_PICK_CLEAR = "au:pick:clear"
CB_ALLOWED_REMOVE_MENU = "au:rm:menu"
CB_ALLOWED_REMOVE = "au:rm:"  # au:rm:<user_id>
CB_ALLOWED_REFRESH = "au:ref"
CB_ALLOWED_BACK = "au:back"

# Apps manager
CB_APPS_REFRESH = "am:ref"
CB_APPS_BACK = "am:back"
CB_APPS_OPEN = "am:open:"  # am:open:<app_name>
CB_APPS_RUN = "am:run:"  # am:run:<app_name>
CB_APPS_CONFIGURE = "am:cfg:"  # am:cfg:<app_name>
CB_APPS_TOGGLE = "am:tg:"  # am:tg:<app_name>
CB_APPS_LOOPER_OPEN = "am:loop:open"
CB_APPS_LOOPER_PLAN = "am:loop:plan:"  # am:loop:plan:<index>
CB_APPS_LOOPER_PLAN_MANUAL = "am:loop:plan:manual"
CB_APPS_LOOPER_INTERVAL = "am:loop:iv:"  # am:loop:iv:<seconds|custom>
CB_APPS_LOOPER_LIMIT = "am:loop:lim:"  # am:loop:lim:<seconds|custom>
CB_APPS_LOOPER_KEYWORD = "am:loop:key"
CB_APPS_LOOPER_INSTRUCTIONS = "am:loop:ins"
CB_APPS_LOOPER_START = "am:loop:start"
CB_APPS_LOOPER_STOP = "am:loop:stop"

# Session lifecycle manager
CB_SESSION_REFRESH = "se:ref"
CB_SESSION_FORK = "se:fork"
CB_SESSION_RESUME = "se:res:"  # se:res:<index>
CB_SESSION_RESUME_LATEST = "se:res:latest"
CB_SESSION_ROLLBACK = "se:rb:"  # se:rb:<count>
CB_SESSION_PAGE = "se:pg:"  # se:pg:<index>

# Worktree manager
CB_WORKTREE_NEW = "wt:new"
CB_WORKTREE_REFRESH = "wt:ref"
CB_WORKTREE_FOLD_MENU = "wt:fold:menu"
CB_WORKTREE_FOLD_TOGGLE = "wt:fold:tg:"  # wt:fold:tg:<index>
CB_WORKTREE_FOLD_RUN = "wt:fold:run"
CB_WORKTREE_FOLD_BACK = "wt:fold:back"
