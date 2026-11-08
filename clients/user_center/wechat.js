(() => {
  "use strict";
  const el = (id) => document.getElementById(id);
  let snapshot = null;
  let sessionId = "";
  let pollTimer = null;
  let expiresAt = 0;
  let busy = false;
  let polling = false;
  let ownerId = "";

  function show(text, error = false) {
    el("wechat-status").textContent = text;
    el("wechat-status").className = "status " + (error ? "err" : "ok");
  }

  async function request(path, method = "GET", body) {
    let auth;
    try { auth = JSON.parse(localStorage.getItem("edu_auth") || "null"); } catch (_) { auth = null; }
    if (!auth || !auth.token || (ownerId && auth.user?.id !== ownerId)) throw new Error("登录账号已变更，请刷新页面");
    const response = await fetch(path, {
      method, cache: "no-store", signal: AbortSignal.timeout(15000),
      headers: { Authorization: "Bearer " + auth.token, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (response.status === 401) throw new Error("登录已过期，请重新登录");
    if (response.status === 404) throw new Error("当前后端尚未加载微信绑定接口，请联系管理员重启本项目后端，再点击刷新状态；重复登录不能解决此问题");
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "请求失败，请稍后重试");
    return data;
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  function clearSession() {
    stopPolling();
    sessionId = "";
    el("wechat-session").hidden = true;
    el("wechat-confirm").hidden = true;
    el("wechat-qr").removeAttribute("src");
    el("wechat-open").href = "#";
  }

  function render(data) {
    snapshot = data;
    const config = data.official || {};
    el("wechat-setup").hidden = !!config.bind_ready;
    el("wechat-setup").textContent = config.bind_ready ? "" : (config.bind_error || "服务号尚未就绪") + " 请管理员按提示配置项目服务号的 AppID、AppSecret 和公网 HTTPS 网页授权地址，重启服务后刷新本页。";
    el("wechat-bind").disabled = !config.bind_ready || busy;
    el("wechat-bind").textContent = data.bound ? "重新绑定微信" : "绑定微信";
    el("wechat-toggle").hidden = !data.bound || !data.identity_valid;
    el("wechat-unbind").hidden = !data.bound;
    el("wechat-toggle").textContent = data.enabled ? "暂停微信通知" : "开启微信通知";
    el("wechat-summary").textContent = !data.bound ? "尚未绑定微信 · 服务号：" + (config.name || "学情助手")
      : data.account_changed ? "服务号已变更，需要重新绑定微信。"
      : "微信已绑定 · " + (data.enabled ? "已同意接收学习情况和课程更新" : "微信通知已暂停")
        + " · 服务号：" + (config.name || "学情助手");
    el("wechat-children").textContent = "接收本账号的学习情况与课程更新，无需关联其他账号。" + (data.scheduler_enabled
      ? " 每天 " + data.report_time + "（" + data.report_timezone + "）汇总已结束课堂学情。"
      : " 当前定时推送尚未开启，请联系管理员。") + (!config.send_ready ? " " + (config.send_error || "服务号通知未就绪") : "");
    if (!config.bind_ready) show(config.bind_error || "项目服务号尚未配置，请联系管理员", true);
  }

  async function refresh() {
    render(await request("/api/wechat/me"));
  }

  async function run(action) {
    if (busy) return;
    busy = true;
    for (const id of ["wechat-bind", "wechat-toggle", "wechat-unbind", "wechat-confirm", "wechat-cancel", "wechat-refresh"]) el(id).disabled = true;
    try { await action(); }
    catch (error) { show(error.message || "操作失败，请重试", true); }
    finally {
      busy = false;
      for (const id of ["wechat-bind", "wechat-toggle", "wechat-unbind", "wechat-confirm", "wechat-cancel", "wechat-refresh"]) el(id).disabled = false;
      if (snapshot) el("wechat-bind").disabled = !snapshot.official?.bind_ready;
    }
  }

  async function poll() {
    if (!sessionId || polling || document.hidden) return;
    if (Date.now() / 1000 >= expiresAt) {
      clearSession();
      show("二维码已过期，请重新点击绑定微信。", true);
      return;
    }
    polling = true;
    const current = sessionId;
    try {
      const data = await request("/api/wechat/me/bind/" + encodeURIComponent(current));
      if (sessionId !== current) return;
      if (data.status === "authorized") {
        stopPolling();
        el("wechat-confirm").hidden = false;
        el("wechat-qr").removeAttribute("src");
        show("微信已授权。请确认刚才由您本人扫码，然后点击确认绑定；不是本人请取消。", false);
      } else if (data.status === "failed" || data.status === "consumed") {
        clearSession();
        show("授权未完成或会话已使用，请重新绑定。", true);
      } else {
        show(data.status === "waiting" ? "等待本人微信扫码授权…" : "正在等待微信授权返回…");
      }
    } catch (error) {
      if (sessionId !== current) return;
      stopPolling();
      show(error.message + "，可刷新页面后重新发起绑定。", true);
    } finally { polling = false; }
  }

  window.initializeWechatSettings = async (user) => {
    ownerId = user.id;
    el("wechat-card").hidden = false;
    el("wechat-admin-entry").hidden = user.role !== "admin";
    try { await refresh(); } catch (error) { show(error.message, true); }
    try {
      const data = await request("/api/wechat/me/course-updates");
      const container = el("my-course-updates");
      container.replaceChildren();
      if (!data.notices.length) container.textContent = "暂无课程更新。";
      for (const notice of data.notices) {
        const article = document.createElement("article");
        const title = document.createElement("h3");
        const content = document.createElement("p");
        title.textContent = notice.title;
        content.textContent = notice.content;
        content.style.whiteSpace = "pre-wrap";
        article.append(title, content);
        container.append(article);
      }
    } catch (_) { el("my-course-updates").textContent = "课程更新暂时无法读取，请刷新重试。"; }
  };

  el("wechat-bind").addEventListener("click", () => run(async () => {
    clearSession();
    const data = await request("/api/wechat/me/bind", "POST");
    sessionId = data.session_id;
    expiresAt = data.expires_at;
    el("wechat-qr").src = data.qr_data_url;
    el("wechat-open").href = data.authorize_url;
    el("wechat-session").hidden = false;
    el("wechat-confirm").hidden = true;
    show("请使用本人微信扫一扫；微信内访问可直接打开授权页。");
    pollTimer = setInterval(poll, 2500);
  }));

  el("wechat-confirm").addEventListener("click", () => run(async () => {
    if (!sessionId || !confirm("确认刚才是您本人微信扫码，并同意服务号向该微信发送本账号的学习情况和课程更新？")) return;
    const data = await request("/api/wechat/me/confirm", "POST", { session_id: sessionId, consent: true });
    clearSession();
    render(data);
    show("微信绑定已保存。请关注项目服务号，并确认通知已开启。" + (!data.official.send_ready ? " 管理员仍需完成学情模板配置。" : ""));
  }));

  el("wechat-cancel").addEventListener("click", () => run(async () => {
    await request("/api/wechat/me/bind", "DELETE");
    clearSession();
    show("已取消本次绑定，原有绑定不会更改。");
  }));

  el("wechat-toggle").addEventListener("click", () => run(async () => {
    const data = await request("/api/wechat/me", "PATCH", { enabled: !snapshot.enabled });
    render(data);
    show(data.enabled ? "已开启学习情况和课程更新通知。" : "已暂停通知，微信绑定和学习记录保留。");
  }));

  el("wechat-unbind").addEventListener("click", () => run(async () => {
    if (!confirm("确认解除本账号的微信绑定并停止学习情况和课程更新推送？学习记录不会删除。")) return;
    const data = await request("/api/wechat/me", "DELETE");
    clearSession();
    render(data);
    show("已解除微信绑定并停止服务号推送。", false);
  }));

  el("wechat-refresh").addEventListener("click", () => run(refresh));
  window.addEventListener("pagehide", stopPolling);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && sessionId) poll(); });
  window.addEventListener("storage", (event) => {
    if (event.key === "edu_auth") {
      clearSession();
      el("wechat-card").hidden = true;
    }
  });
})();
