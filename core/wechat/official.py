"""项目服务号：网页授权、应用令牌与学情模板消息。"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from config.settings import settings

API_BASE = "https://api.weixin.qq.com"
CALLBACK_PATH = "/api/wechat/official/callback"
DEMO_PATH = "/api/wechat/official/demo"
DEMO_OPENID = "demo_openid_local"
DEMO_ENVS = {"local", "dev", "development", "test"}
DEMO_BIND_NOTICE = "本地演示模式：未配置服务号，使用模拟微信身份完成绑定，不会连接微信服务器"
VARIABLES = {"student_name", "date", "summary", "sessions", "minutes", "completion", "mastery", "interaction"}
_token_lock = threading.Lock()
_token_cache: tuple[str, str, float] = ("", "", 0.0)


class WeChatError(ValueError):
    pass


class DeliveryUncertain(WeChatError):
    pass


def public_base_url() -> str:
    base = (settings.wechat_public_base_url or "").strip().rstrip("/")
    try:
        parsed = urlparse(base)
        valid = (parsed.scheme == "https" and bool(parsed.hostname)
                 and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                 and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment and parsed.path in ("", "/"))
    except ValueError:
        valid = False
    if not valid:
        raise WeChatError("服务号需要配置用户可访问的 HTTPS 根地址 WECHAT_PUBLIC_BASE_URL")
    return base


def bind_demo() -> bool:
    """本地未接入服务号时用模拟身份走通绑定；生产环境与已配置凭据时始终为 False。"""
    if not settings.wechat_bind_demo_enable:
        return False
    if (settings.env or "").strip().lower() not in DEMO_ENVS:
        return False
    return not settings.wechat_official_app_id.strip() and not settings.wechat_official_app_secret.strip()


def bind_base_url() -> str:
    """绑定链路的对外根地址：演示模式允许本机 HTTP，正式模式必须是公网 HTTPS。"""
    if not bind_demo():
        return public_base_url()
    base = (settings.wechat_public_base_url or "").strip().rstrip("/")
    try:
        parsed = urlparse(base)
        if (parsed.scheme in ("http", "https") and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment and parsed.path in ("", "/")):
            return base
    except ValueError:
        pass
    return f"http://127.0.0.1:{settings.port}"


def template_fields() -> dict[str, str]:
    try:
        fields = json.loads(settings.wechat_official_template_fields or "{}")
    except (ValueError, TypeError) as err:
        raise WeChatError("服务号学情模板字段映射不是有效 JSON") from err
    if not isinstance(fields, dict) or not fields or len(fields) > 20:
        raise WeChatError("请按服务号后台的学情模板配置字段映射")
    for key, value in fields.items():
        if (not re.fullmatch(r"(?:thing|character_string|number|time|date)\d+", key)
                or not isinstance(value, str) or value not in VARIABLES):
            raise WeChatError("学情模板字段或学情变量不受支持，请检查模板映射")
    return fields


def configuration() -> dict[str, Any]:
    bind_error = ""
    demo = bind_demo()
    try:
        if not settings.wechat_assistant_enable or not settings.auth_enable:
            raise WeChatError("微信助手或账号体系尚未启用")
        if demo:
            bind_base_url()
        else:
            if not settings.wechat_official_app_id.strip() or not settings.wechat_official_app_secret.strip():
                raise WeChatError("项目服务号尚未配置，请联系管理员；用户无需填写密钥")
            public_base_url()
    except WeChatError as err:
        bind_error = str(err)
    send_error = bind_error
    if not send_error and demo:
        send_error = DEMO_BIND_NOTICE
    if not send_error:
        try:
            if not settings.wechat_official_template_id.strip():
                raise WeChatError("服务号尚未配置学情通知模板")
            template_fields()
        except WeChatError as err:
            send_error = str(err)
    course_error = bind_error
    if not course_error and demo:
        course_error = DEMO_BIND_NOTICE
    if not course_error:
        try:
            if not settings.wechat_official_course_template_id.strip():
                raise WeChatError("服务号尚未配置课程更新通知模板")
            course_template_fields()
        except WeChatError as err:
            course_error = str(err)
    return {"name": settings.wechat_official_name, "bind_ready": not bool(bind_error),
            "send_ready": not bool(send_error), "bind_error": bind_error, "send_error": send_error,
            "course_ready": not bool(course_error), "course_error": course_error, "demo": demo}


def _request(path: str, *, params: dict | None = None, body: dict | None = None) -> dict:
    try:
        with httpx.Client(timeout=12.0, follow_redirects=False) as client:
            if body is None:
                resp = client.get(API_BASE + path, params=params)
            else:
                resp = client.post(API_BASE + path, params=params, json=body)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as err:
        if path == "/cgi-bin/message/template/send":
            raise DeliveryUncertain("微信发送结果不明，请先核对接收端，不要直接重复发送") from err
        raise WeChatError("微信服务请求失败，请检查网络和服务号配置") from err
    if not isinstance(data, dict):
        if path == "/cgi-bin/message/template/send":
            raise DeliveryUncertain("微信发送返回无效响应，结果不明，请核对接收端")
        raise WeChatError("微信服务返回了无效响应")
    if "errcode" in data and str(data["errcode"]) != "0":
        code = str(data["errcode"])
        reasons = {"40164": "服务端出口 IP 不在微信白名单内", "40029": "微信授权码无效，请重新扫码",
                   "40163": "微信授权码已使用，请重新扫码", "43004": "请先关注项目服务号再接收学情",
                   "40037": "学情通知模板无效", "45009": "服务号调用达到限额",
                   "40003": "微信身份与服务号不匹配", "48001": "服务号缺少所需接口权限"}
        reason = reasons.get(code, "请检查服务号凭据、权限和模板配置")
        raise WeChatError(f"微信接口拒绝请求（{code if code.isdigit() else '未知'}）：{reason}")
    return data


def authorization_url(state: str) -> str:
    if bind_demo():
        return bind_base_url() + DEMO_PATH + "?" + urlencode({"state": state})
    return "https://open.weixin.qq.com/connect/oauth2/authorize?" + urlencode({
        "appid": settings.wechat_official_app_id.strip(),
        "redirect_uri": public_base_url() + CALLBACK_PATH,
        "response_type": "code", "scope": "snsapi_base", "state": state,
    }) + "#wechat_redirect"


def exchange_openid(code: str) -> str:
    if bind_demo():
        return DEMO_OPENID
    data = _request("/sns/oauth2/access_token", params={
        "appid": settings.wechat_official_app_id.strip(),
        "secret": settings.wechat_official_app_secret.strip(),
        "code": code, "grant_type": "authorization_code",
    })
    openid = data.get("openid")
    if not isinstance(openid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", openid):
        raise WeChatError("微信未返回有效身份，请重新发起绑定")
    return openid


def access_token() -> str:
    global _token_cache
    appid = settings.wechat_official_app_id.strip()
    secret = settings.wechat_official_app_secret.strip()
    cache_key = hashlib.sha256((appid + ":" + secret).encode()).hexdigest()
    with _token_lock:
        if _token_cache[0] == cache_key and _token_cache[2] > time.time():
            return _token_cache[1]
        data = _request("/cgi-bin/stable_token", body={
            "grant_type": "client_credential", "appid": appid, "secret": secret, "force_refresh": False,
        })
        token = data.get("access_token")
        try:
            ttl = int(data.get("expires_in", 0))
        except (ValueError, TypeError):
            ttl = 0
        if not isinstance(token, str) or not token or ttl <= 0:
            raise WeChatError("微信未返回有效应用令牌")
        _token_cache = (cache_key, token, time.time() + max(0, ttl - 120))
        return token


def send_report(openid: str, report: dict[str, Any]) -> str:
    config = configuration()
    if not config["send_ready"]:
        raise WeChatError(config["send_error"])
    student = report.get("student", {})
    summary = report.get("report", {})
    if not student.get("id") or not report.get("has_course", True):
        raise WeChatError("服务号仅发送用户实际学习情况，不发送伪造学情")

    def percent(value: Any) -> str:
        return "未验证" if value is None else f"{round(float(value) * 100)}%"

    values = {
        "student_name": str(student.get("name") or "学生"), "date": str(report.get("date") or ""),
        "summary": str(summary.get("status") or "学情已更新"),
        "sessions": str(summary.get("sessions", 0)), "minutes": str(summary.get("minutes", 0)),
        "completion": percent(summary.get("completion_rate")), "mastery": percent(summary.get("mastery")),
        "interaction": percent(summary.get("interaction_rate")),
    }
    fields = {key: {"value": values[value][:20 if key.startswith("thing") else 32]}
              for key, value in template_fields().items()}
    url = public_base_url() + "/app/learning_center/?" + urlencode({"student_id": student["id"]})
    data = _request("/cgi-bin/message/template/send", params={"access_token": access_token()}, body={
        "touser": openid, "template_id": settings.wechat_official_template_id.strip(), "url": url, "data": fields,
    })
    if str(data.get("errcode")) != "0" or not data.get("msgid"):
        raise WeChatError("微信未确认学情通知已受理")
    return str(data["msgid"])


def course_template_fields() -> dict[str, str]:
    try:
        fields = json.loads(settings.wechat_official_course_template_fields or "{}")
    except (ValueError, TypeError) as err:
        raise WeChatError("课程更新模板字段映射不是有效 JSON") from err
    variables = {"user_name", "title", "summary", "date"}
    if not isinstance(fields, dict) or not fields or len(fields) > 20:
        raise WeChatError("请配置课程更新模板字段映射")
    for key, value in fields.items():
        if (not re.fullmatch(r"(?:thing|character_string|number|time|date)\d+", key)
                or not isinstance(value, str) or value not in variables):
            raise WeChatError("课程更新模板字段或变量不受支持")
    return fields


def send_course_update(openid: str, notice: dict[str, Any]) -> str:
    config = configuration()
    if not config["course_ready"]:
        raise WeChatError(config["course_error"])
    if not notice.get("notice_id") or not notice.get("student", {}).get("id"):
        raise WeChatError("需要已保存的用户课程更新")
    values = {"user_name": str(notice["student"].get("name") or "用户"),
              "title": str(notice.get("title") or "课程更新"),
              "summary": str(notice.get("content") or ""), "date": str(notice.get("date") or "")}
    fields = {key: {"value": values[value][:20 if key.startswith("thing") else 32]}
              for key, value in course_template_fields().items()}
    data = _request("/cgi-bin/message/template/send", params={"access_token": access_token()}, body={
        "touser": openid, "template_id": settings.wechat_official_course_template_id.strip(),
        "url": public_base_url() + "/app/user_center/#course-updates", "data": fields,
    })
    if str(data.get("errcode")) != "0" or not data.get("msgid"):
        raise DeliveryUncertain("微信未确认课程通知已受理，结果不明，请核对接收端")
    return str(data["msgid"])
