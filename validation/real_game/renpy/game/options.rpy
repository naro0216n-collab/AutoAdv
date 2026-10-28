define config.name = "AutoAdv Validation Game"
define config.version = "1"

define build.name = "AutoAdvValidationGame"
define build.directory_name = "AutoAdvValidationGame"
define build.executable_name = "AutoAdvValidationGame"
# The fixture intentionally contains Japanese dialogue but does not carry a full
# generated gui.rpy. Use Ren'Py's bundled CJK font instead of the Latin-only
# default so the validation game renders the same text LunaHook observes.
style default:
    font "SourceHanSansLite.ttf"
