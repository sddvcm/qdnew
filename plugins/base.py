"""插件基类 — 所有签到插件必须继承"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional


@dataclass
class FormField:
    key: str
    label: str
    type: str = "text"
    required: bool = False
    default: Any = None
    placeholder: str = ""
    options: List[Dict] = field(default_factory=list)
    help_text: str = ""
    sensitive: bool = False

    def to_dict(self):
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "placeholder": self.placeholder,
            "options": self.options,
            "help_text": self.help_text,
            "sensitive": self.sensitive,
        }


@dataclass
class CheckinResult:
    success: bool
    message: str
    extra: Dict[str, Any] = field(default_factory=dict)
    cookie: str = ""  # 登录类插件可在签到成功后回填最新 Cookie，引擎会加密存档

    def to_dict(self):
        return {"success": self.success, "message": self.message, "extra": self.extra}


class BasePlugin(ABC):
    name: str = ""
    display_name: str = ""
    description: str = ""
    version: str = "1.0"
    plugin_type: str = "http"
    author: str = ""

    form_schema: List[FormField] = []

    def on_load(self):
        pass

    def on_unload(self):
        pass

    def validate_params(self, params: dict) -> tuple:
        for field in self.form_schema:
            if field.required and (field.key not in params or not params[field.key]):
                return False, f"缺少必填参数: {field.label}"
        return True, ""

    @abstractmethod
    def checkin(self, task_config: Dict[str, Any]) -> CheckinResult:
        ...

    def pre_checkin(self, task_config: dict) -> Optional[CheckinResult]:
        return None

    def post_checkin(self, task_config: dict, result: CheckinResult) -> CheckinResult:
        return result
