(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const labels = { pending: "待发送", sending: "正在发送 / 等待核对", sent: "渠道已受理", simulated: "本地模拟（未发微信）", skipped: "已跳过", failed: "失败", unknown: "结果不明，需核对", cancelled: "已取消" };
  let users = [], notices = [], configuration = {}, summary = {}, busy = false;
  const selected = () => users.find((u) => u.id === $("recipient").value);

  async function api(path, method = "GET", body) {
    let auth;
    try { auth = JSON.parse(localStorage.getItem("edu_auth") || "null"); } catch (_) { auth = null; }
    if (!auth?.token) { gate("请使用管理员账号登录。"); throw new Error("需要管理员登录"); }
    const response = await fetch(path, { method, cache: "no-store", signal: AbortSignal.timeout(15000),
      headers: { Authorization: "Bearer " + auth.token, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    const data = await response.json().catch(() => ({}));
    if (response.status === 401 || response.status === 403) gate("登录已失效或当前账号不是管理员。");
    if (!response.ok) throw new Error(response.status === 404 ? "当前后端未加载新接口，请管理员重启服务。" : typeof data.detail === "string" ? data.detail : "请求失败，请检查输入后重试");
    return data;
  }

  function gate(text) { $("workspace").hidden = true; $("gate").hidden = false; $("gate-status").textContent = text; }
  function setAvailability() {
    const user = selected();
    const ready = !!user?.notifications_enabled;
    $("recipient").disabled = busy;
    $("p-send").disabled = busy || !ready || !configuration.send_ready;
    $("c-save").disabled = busy || !ready || !configuration.course_ready || !summary.course_scheduler_enable;
    $("recipient-status").textContent = !user ? "还没有用户，请先注册普通用户账号。" : !user.bound ? "该用户未绑定微信，请使用该账号进入用户中心点击绑定微信。" : !user.identity_valid ? "服务号已变更，请用户重新绑定微信。" : !user.notifications_enabled ? "用户已暂停微信通知，管理员不能代为开启。" : "微信已绑定，通知已开启。学习情况和课程更新发送到该用户本人微信。";
  }
  async function run(statusId, action) {
    if (busy) return;
    busy = true;
    setAvailability();
    try { await action(); } catch (error) { $(statusId).textContent = error.message || "操作失败"; }
    finally { busy = false; setAvailability(); }
  }
  function newCourse() { $("course-form").reset(); $("notice-id").value = ""; $("c-status").textContent = ""; $("course-preview").textContent = "保存只安排发送，不会立即推送。"; }
  function body() {
    if (!selected()) throw new Error("请先选择用户");
    const value = $("course-time").value;
    if (!value) throw new Error("请选择计划发送时间");
    return { user_id: selected().id, title: $("course-title").value.trim(), content: $("course-content").value.trim(), send_at: new Date(value).toISOString() };
  }
  function localTime(timestamp) { const d = new Date(timestamp * 1000); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16); }
  async function loadNotices() {
    notices = selected() ? (await api("/api/admin/wechat/course-updates?user_id=" + encodeURIComponent(selected().id))).notices : [];
    $("notice-rows").innerHTML = notices.length ? notices.map((n) => '<tr><td>' + esc(n.title) + '</td><td>' + esc(new Date(n.send_at * 1000).toLocaleString("zh-CN", { hour12: false })) + '</td><td>' + esc(labels[n.status] || n.status) + '</td><td>' + esc(n.error || "—") + '</td><td>' + (n.status === "pending" ? '<button type="button" data-edit="' + esc(n.id) + '">编辑</button> <button type="button" data-cancel="' + esc(n.id) + '">取消</button>' : "—") + '</td></tr>').join("") : '<tr><td colspan="5">暂无课程更新。</td></tr>';
  }
  async function deliveries() {
    const data = await api("/api/admin/wechat/deliveries?date=" + encodeURIComponent($("d-date").value));
    $("delivery-rows").innerHTML = data.deliveries.length ? data.deliveries.map((r) => '<tr><td>' + esc(r.date) + '</td><td>' + esc(r.guardian) + '</td><td>' + esc(r.student) + '</td><td>' + esc(r.channel) + '</td><td>' + esc(r.status_label || labels[r.status]) + '</td><td>' + esc(r.error || "—") + '</td></tr>').join("") : '<tr><td colspan="6">暂无学习情况发送记录。</td></tr>';
  }
  async function refresh() {
    const old = $("recipient").value;
    const data = await api("/api/admin/wechat/options");
    summary = await api("/api/admin/wechat/status");
    users = data.users || [];
    configuration = summary.official || {};
    $("recipient").innerHTML = users.map((u) => '<option value="' + esc(u.id) + '">' + esc((u.display_name || u.username) + '（' + u.username + '） · ' + (u.notifications_enabled ? "微信已就绪" : u.bound ? "已绑定 / 未就绪" : "未绑定")) + '</option>').join("");
    if (users.some((u) => u.id === old)) $("recipient").value = old;
    $("channel-status").textContent = "微信绑定：" + (configuration.bind_ready ? "已就绪" : configuration.bind_error || "未配置") + "；学情通知：" + (configuration.send_ready ? "已就绪" : configuration.send_error || "未配置") + "；课程通知：" + (configuration.course_ready ? "已就绪" : configuration.course_error || "未配置");
    $("schedule-status").textContent = "日报定时：" + (summary.scheduler_enable ? summary.report_time + "（" + summary.report_timezone + "）" : "关闭") + "；课程更新定时：" + (summary.course_scheduler_enable ? "开启，约每分钟检查" : "关闭");
    if (!$("p-date").value) $("p-date").value = summary.date;
    if (!$("d-date").value) $("d-date").value = summary.date;
    $("workspace").hidden = false;
    $("gate").hidden = true;
    await Promise.all([loadNotices(), deliveries()]);
    setAvailability();
    $("status").textContent = "用户、微信配置及发送记录已更新。";
  }
  $("refresh").onclick = () => run("status", refresh);
  $("recipient").onchange = () => run("status", async () => { newCourse(); $("preview").textContent = "选择用户后生成预览。"; $("p-status").textContent = ""; await loadNotices(); });
  $("p-preview").onclick = () => run("p-status", async () => {
    if (!selected()) throw new Error("请先选择用户");
    const data = await api("/api/admin/wechat/preview?student_id=" + encodeURIComponent(selected().id) + "&date=" + encodeURIComponent($("p-date").value));
    $("preview").textContent = data.text;
    $("p-status").textContent = data.has_course ? "预览已生成，尚未发送。" : "当天没有已结束课堂，发送会被跳过。";
  });
  $("p-send").onclick = () => run("p-status", async () => {
    const user = selected();
    if (!user?.notifications_enabled || !configuration.send_ready) throw new Error("用户绑定或服务号模板尚未就绪");
    if (!confirm("确认向“" + (user.display_name || user.username) + "”本人的微信发送所选日期的学习情况？")) return;
    const data = await api("/api/admin/wechat/send", "POST", { user_id: user.id, date: $("p-date").value });
    $("p-status").textContent = data.skipped ? "已跳过：" + data.reason : !data.ok ? "失败：" + data.error : data.simulated ? "仅本地模拟，未发微信。" : "渠道已受理，请核对微信接收端。";
    await deliveries();
  });
  $("c-preview").onclick = () => run("c-status", async () => {
    const data = await api("/api/admin/wechat/course-updates/preview", "POST", body());
    $("course-preview").textContent = data.text;
    $("c-status").textContent = "预览已生成，未保存、未发送。";
  });
  $("course-form").onsubmit = (event) => { event.preventDefault(); run("c-status", async () => {
    const payload = body();
    if (!confirm("确认保存并在所选时间向“" + (selected().display_name || selected().username) + "”发送此课程更新？")) return;
    const id = $("notice-id").value;
    await api("/api/admin/wechat/course-updates" + (id ? "/" + encodeURIComponent(id) : ""), id ? "PUT" : "POST", payload);
    newCourse(); $("c-status").textContent = "已保存，将在设定时间检查发送；当前未发送消息。";
    await loadNotices();
  }); };
  $("c-new").onclick = newCourse;
  $("notice-rows").onclick = (event) => {
    const edit = event.target.closest("[data-edit]");
    const cancel = event.target.closest("[data-cancel]");
    if (busy) return;
    if (edit) { const n = notices.find((n) => n.id === edit.dataset.edit); if (!n) return; $("notice-id").value = n.id; $("course-title").value = n.title; $("course-content").value = n.content; $("course-time").value = localTime(n.send_at); $("course-title").focus(); }
    if (cancel) run("c-status", async () => { if (!confirm("取消这条待发送的更新？")) return; await api("/api/admin/wechat/course-updates/" + encodeURIComponent(cancel.dataset.cancel), "DELETE"); if ($("notice-id").value === cancel.dataset.cancel) newCourse(); await loadNotices(); });
  };
  $("d-refresh").onclick = () => run("status", deliveries);
  $("d-run").onclick = () => run("status", async () => { if (!confirm("运行已启用通知用户的今日日报？这将发送真实学习情况，已发送的会跳过。")) return; const d = await api("/api/admin/wechat/run", "POST"); $("status").textContent = "渠道受理 " + d.real_sent + " 条；模拟 " + d.simulated + " 条；失败 " + d.failed + " 条。"; await deliveries(); });
  run("status", refresh);
})();
