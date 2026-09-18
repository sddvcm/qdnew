"""插件发现、加载、热重载"""
import os
import sys
import json
import importlib
import importlib.util
import traceback
from .database import get_db
from plugins.base import BasePlugin

BUILTIN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "plugins")
# ⚠️ 用户插件目录可通过环境变量重定向。
# 默认 <项目根>/user_plugins（Docker / 本地开发行为不变）；
# 飞牛 fpk 模式下注入 CHECKIN_USER_PLUGINS_DIR=$TRIM_PKGVAR/user_plugins，
# 用户上传的插件落在 fnOS 持久化目录，应用升级不会被冲掉。
USER_DIR = os.environ.get("CHECKIN_USER_PLUGINS_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "user_plugins")

_loaded_plugins: dict = {}


def _discover_plugin_classes(module):
    """从模块中找出所有 BasePlugin 子类"""
    classes = []
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if isinstance(attr, type) and issubclass(attr, BasePlugin) and attr is not BasePlugin:
            classes.append(attr)
    return classes


def _load_from_dir(directory: str, builtin: bool = False):
    """从目录加载所有插件"""
    if not os.path.isdir(directory):
        return

    sys.path.insert(0, os.path.dirname(directory))

    for filename in os.listdir(directory):
        if filename.startswith("_") or not filename.endswith(".py"):
            continue
        mod_name = filename[:-3]
        full_path = os.path.join(directory, filename)
        try:
            spec = importlib.util.spec_from_file_location(f"plugins.{mod_name}", full_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for cls in _discover_plugin_classes(module):
                instance = cls()
                if instance.name and instance.name not in _loaded_plugins:
                    _loaded_plugins[instance.name] = instance
                    _sync_to_db(instance, builtin)
        except Exception:
            print(f"[PluginLoader] Failed to load {filename}: {traceback.format_exc()}")


def _sync_to_db(plugin: BasePlugin, builtin: bool):
    """将插件信息同步到数据库"""
    db = get_db()
    existing = db.execute("SELECT id FROM plugins WHERE name=?", (plugin.name,)).fetchone()
    form_schema = json.dumps([f.to_dict() for f in plugin.form_schema], ensure_ascii=False)
    if existing:
        db.execute(
            "UPDATE plugins SET display_name=?, description=?, version=?, plugin_type=?, "
            "author=?, form_schema=?, updated_at=datetime('now','localtime') WHERE name=?",
            (plugin.display_name, plugin.description, plugin.version,
             plugin.plugin_type, plugin.author, form_schema, plugin.name))
    else:
        db.execute(
            "INSERT INTO plugins (name, display_name, description, version, plugin_type, "
            "author, builtin, form_schema) VALUES (?,?,?,?,?,?,?,?)",
            (plugin.name, plugin.display_name, plugin.description, plugin.version,
             plugin.plugin_type, plugin.author, int(builtin), form_schema))
    db.commit()
    db.close()


def load_all_plugins():
    """启动时加载所有插件"""
    global _loaded_plugins
    _loaded_plugins = {}
    _load_from_dir(BUILTIN_DIR, builtin=True)
    _load_from_dir(USER_DIR, builtin=False)


def get_plugin(name: str) -> BasePlugin:
    """获取已加载的插件实例"""
    return _loaded_plugins.get(name)


def get_all_plugins() -> dict:
    """获取所有已加载插件"""
    return _loaded_plugins


def load_user_plugin(source_code: str, filename: str) -> tuple:
    """动态加载用户上传的插件代码，返回 (success, message, plugin_info)"""
    if not filename.endswith(".py"):
        filename = filename + ".py"

    safe_name = filename.replace("/", "_").replace("\\", "_")
    filepath = os.path.join(USER_DIR, safe_name)

    if os.path.exists(filepath):
        return False, f"插件文件 {safe_name} 已存在", None

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(source_code)

    try:
        spec = importlib.util.spec_from_file_location(f"user_plugins.{safe_name[:-3]}", filepath)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        classes = _discover_plugin_classes(module)

        if not classes:
            os.remove(filepath)
            return False, "未找到有效的插件类（需继承 BasePlugin）", None

        instance = classes[0]()
        if not instance.name:
            os.remove(filepath)
            return False, "插件类缺少 name 属性", None

        _loaded_plugins[instance.name] = instance
        _sync_to_db(instance, builtin=False)

        return True, f"插件 {instance.display_name} 加载成功", {
            "name": instance.name,
            "display_name": instance.display_name,
            "plugin_type": instance.plugin_type,
            "filename": safe_name,
        }
    except Exception as e:
        if os.path.exists(filepath):
            os.remove(filepath)
        return False, f"加载失败: {str(e)}", None


def delete_user_plugin(plugin_name: str) -> tuple:
    """删除用户插件"""
    plugin = _loaded_plugins.get(plugin_name)
    if not plugin:
        return False, "插件不存在"

    filepath = os.path.join(USER_DIR, f"{plugin_name}.py")
    if os.path.exists(filepath):
        os.remove(filepath)

    del _loaded_plugins[plugin_name]

    db = get_db()
    db.execute("DELETE FROM plugins WHERE name=? AND builtin=0", (plugin_name,))
    db.commit()
    db.close()

    return True, f"插件 {plugin.display_name} 已删除"
