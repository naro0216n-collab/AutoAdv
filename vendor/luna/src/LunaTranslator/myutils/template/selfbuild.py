import requests
from translator.basetranslator import basetrans


class TS(basetrans):
    def translate(self, content: str):
        # 在这里编写
        return content

    def init(self):
        pass

    def langmap(self):
        # The mapping between standard language code and API language code, if not declared, defaults to using standard language code.
        # But the exception is cht. If api support cht, if must be explicitly declared the support of cht, otherwise it will translate to chs and then convert to cht.
        return {}
