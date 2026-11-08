(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const base = "/api/admin/wechat/course-robot";
  let robots = [], courses = [], selectedId = "", busy = false;
  let ownerToken = "";
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const statusLabels = { sending: "发送中", sent: "渠道已受理", simulated: "模拟成功 · 未发微信", skipped: "无课程或已停用", failed: "失败 · 不自动重试", unknown: "结果不明 · 请核对群消息" };

  function auth() { try { return JSON.parse(localStorage.getItem("edu_auth") || "null"); } catch { return null; } }
  function gate(kind = "auth", text = "课程、家长群和发送时间仅允许管理员管理。") {
    $("main").hidden = true; $("gate").hidden = false;
    $("gate-title").textContent = kind === "backend" ? "课程通知接口尚未加载" : kind === "forbidden" ? "当前账号无管理权限" : kind === "service" ? "服务暂时不可用" : "需要管理员登录";
    $("gate-status").textContent = text;
    $("login-link").hidden = kind === "backend" || kind === "service";
    $("retry-load").hidden = kind === "auth" || kind === "forbidden";
  }
  function message(text, error = false) { $("global-status").textContent = text; $("global-status").className = "banner" + (error ? " error" : ""); }

  async function api(path, method = "GET", body) {
    const current = auth();
    if (!current?.token || current.token !== ownerToken) { gate(); throw new Error("登录已失效或账号已变更，请重新登录"); }
    const response = await fetch(base + path, {
      method, cache: "no-store", signal: AbortSignal.timeout(20000),
      headers: { Authorization: "Bearer " + ownerToken, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const kind = response.status === 404 ? "backend" : response.status === 401 ? "auth" : response.status === 403 ? "forbidden" : "service";
      const text = response.status === 404 ? "当前服务没有课程通知接口。请在启动后端的终端停止旧服务，再从本项目重新启动；刷新或重复登录不能解决此问题。" : typeof data.detail === "string" ? data.detail : Array.isArray(data.detail) ? data.detail.map((item) => item.msg).join("；") : "请求失败，请重试";
      if (response.status === 401 || response.status === 403) gate(kind, text);
      throw Object.assign(new Error(text), { kind });
    }
    return data;
  }

  async function run(action) {
    if (busy) return;
    busy = true;
    const controls = [...document.querySelectorAll("button, #robot-select")].map((control) => [control, control.disabled]);
    controls.forEach(([control]) => { control.disabled = true; });
    try { await action(); } catch (error) {
      message(error.message || "操作失败，请重试", true);
      if ($("main").hidden) gate(error.kind || "service", error.message || "服务暂时不可用，请刷新重试");
    }
    finally { busy = false; controls.forEach(([control, disabled]) => { control.disabled = disabled; }); }
  }

  function channelChanged() { $("webhook-row").hidden = $("channel").value !== "wecom_webhook"; }

  function fillRobot(robot) {
    $("robot-form").reset();
    $("robot-name").value = robot?.name || "";
    $("channel").value = robot?.channel || "mock";
    $("webhook").value = "";
    $("timezone").value = robot?.timezone || "Asia/Shanghai";
    $("send-times").value = (robot?.send_times || ["08:00", "18:00"]).join(",");
    $("lookahead").value = robot?.lookahead_hours || 24;
    $("grace").value = robot?.grace_minutes || 30;
    $("enabled").checked = !!robot?.enabled;
    document.querySelectorAll("#weekdays input").forEach((input) => { input.checked = (robot?.weekdays || [1, 2, 3, 4, 5, 6, 7]).includes(Number(input.value)); });
    $("robot-state").textContent = robot?.enabled ? (robot.channel === "mock" ? "模拟已启用" : "定时通知已启用") : "未启用";
    $("webhook-hint").textContent = robot?.webhook_configured ? "已保存该群 webhook，留空保留；密钥不会回显。" : "每个接收群配置独立目标，密钥不会回显。不使用全局共享地址。";
    channelChanged();
  }

  function resetCourse() {
    $("course-form").reset(); $("course-id").value = "";
    $("course-form-title").textContent = "安排学生课程";
    $("save-course").textContent = "保存课程安排";
  }

  function robotOptions() {
    $("robot-select").innerHTML = '<option value="">新建接收群</option>' + robots.map((robot) => '<option value="' + escape(robot.id) + '">' + escape(robot.name) + (robot.enabled ? " · 已启用" : " · 已暂停") + '</option>').join("");
    $("robot-select").value = selectedId;
  }

  async function loadRobots() {
    const data = await api("/robots"); robots = data.robots;
    robotOptions();
    const warnings = [];
    if (!data.assistant_enabled) warnings.push("微信助手总开关已关闭");
    if (!data.scheduler_enabled) warnings.push("课程通知定时开关已关闭");
    message(warnings.length ? warnings.join("；") + "，不会自动发送。" : "定时服务已配置。只有启用的接收群会按保存的时间自动发送；mock 只写本地记录。");
  }

  function renderCourses() {
    $("course-count").textContent = "最近 " + courses.length + " 条课程 · 时间按所选群时区展示";
    $("course-rows").innerHTML = courses.length ? courses.map((course) => '<tr><td><strong>' + escape(course.title) + '</strong><small>' + escape(course.student_names.join("、")) + '</small></td><td>' + escape(course.starts_at.slice(0, 16).replace("T", " ")) + '<small>至 ' + escape(course.ends_at.slice(0, 16).replace("T", " ")) + '</small></td><td>' + escape(course.teacher || "未指定老师") + '<small>' + escape(course.location || "未填写地点") + '</small></td><td><span class="tag">' + (course.status === "cancelled" ? "已取消" : "已安排") + '</span></td><td><button type="button" data-edit="' + escape(course.id) + '">编辑</button> <button class="danger" type="button" data-cancel="' + escape(course.id) + '"' + (course.status === "cancelled" ? " disabled" : "") + '>取消课程</button></td></tr>').join("") : '<tr><td colspan="5" class="empty">暂无课程，请在上方安排学生课程。</td></tr>';
  }

  async function loadPreview() {
    if (!selectedId) return;
    const data = await api("/preview?robot_id=" + encodeURIComponent(selectedId));
    $("preview-time").textContent = "下一时段：" + data.slot_at.slice(0, 16).replace("T", " ") + "（" + data.timezone + "）" + (data.enabled ? "" : " · 当前暂停，不会发送");
    $("preview-text").textContent = data.messages.length ? data.messages.join("\n\n──────── 下一条 ────────\n\n") : "该时段汇总范围内没有尚未开始的课程，将记录跳过，不发送空消息。";
  }

  async function loadDetails() {
    resetCourse();
    if (!selectedId) {
      courses = []; renderCourses();
      $("preview-time").textContent = "仅预览，不发送消息。";
      $("preview-text").textContent = "请先保存或选择接收群。";
      $("notice-rows").innerHTML = '<p class="empty">暂无发送记录</p>';
      return;
    }
    const id = encodeURIComponent(selectedId);
    const [courseData, noticeData] = await Promise.all([api("/courses?robot_id=" + id), api("/notices?robot_id=" + id)]);
    courses = courseData.courses; renderCourses();
    $("notice-rows").innerHTML = noticeData.notices.length ? noticeData.notices.map((notice) => '<article class="notice"><strong>' + escape(notice.local_date + " " + notice.slot) + '</strong> <span class="tag">' + escape(statusLabels[notice.status] || notice.status) + '</span><p>渠道：' + escape(notice.channel) + ' · 已受理 ' + escape(notice.accepted_parts) + ' 条' + (notice.error ? ' · ' + escape(notice.error) : '') + '</p><details><summary>查看通知快照</summary><pre>' + escape(notice.messages.join("\n\n")) + '</pre></details></article>').join("") : '<p class="empty">尚无记录；到达已启用的通知时段后生成。</p>';
    await loadPreview();
  }

  $("channel").addEventListener("change", channelChanged);
  $("robot-select").addEventListener("change", () => run(async () => { selectedId = $("robot-select").value; fillRobot(robots.find((robot) => robot.id === selectedId)); await loadDetails(); }));
  $("new-robot").addEventListener("click", () => run(async () => { selectedId = ""; robotOptions(); fillRobot(null); await loadDetails(); $("robot-name").focus(); }));
  $("refresh").addEventListener("click", () => run(async () => { await loadRobots(); fillRobot(robots.find((robot) => robot.id === selectedId)); await loadDetails(); }));
  $("preview").addEventListener("click", () => run(loadPreview));
  $("reset-course").addEventListener("click", resetCourse);

  $("robot-form").addEventListener("submit", (event) => {
    event.preventDefault();
    run(async () => {
      const weekdays = [...document.querySelectorAll("#weekdays input:checked")].map((input) => Number(input.value));
      if (!weekdays.length) throw new Error("请至少选择一个通知星期");
      const body = { name: $("robot-name").value.trim(), channel: $("channel").value, webhook: $("webhook").value.trim(),
        enabled: $("enabled").checked, timezone: $("timezone").value.trim(),
        send_times: $("send-times").value.replace(/，/g, ",").split(",").map((item) => item.trim()).filter(Boolean),
        weekdays, lookahead_hours: Number($("lookahead").value), grace_minutes: Number($("grace").value) };
      if (body.enabled && body.channel !== "mock" && !confirm("启用后将按指定时段向该企业微信群发送课程通知。确认已核对接收群和学生范围？")) return;
      const data = await api("/robots" + (selectedId ? "/" + encodeURIComponent(selectedId) : ""), selectedId ? "PUT" : "POST", body);
      selectedId = data.robot.id;
      await loadRobots(); fillRobot(data.robot); await loadDetails();
      message("已保存机器人和通知时间。保存不会立即发送；只处理保存后到达的时段。" + (body.channel === "mock" ? " 当前为本地模拟，不发送微信。" : ""));
    });
  });

  $("course-form").addEventListener("submit", (event) => {
    event.preventDefault();
    run(async () => {
      if (!selectedId) throw new Error("请先创建并保存接收群机器人");
      const courseId = $("course-id").value;
      const original = courses.find((item) => item.id === courseId);
      const offset = $("utc-offset").value.trim();
      if (offset && !/^[+-](?:[01]\d|2[0-3]):[0-5]\d$/.test(offset)) throw new Error("时区偏移格式应为 +08:00 或 -05:00");
      const timeValue = (id, key) => !offset && original?.[key].slice(0, 16) === $(id).value ? original[key] : $(id).value + offset;
      const body = { robot_id: selectedId, title: $("course-title").value.trim(),
        student_ids: [...$("students").selectedOptions].map((option) => option.value),
        starts_at: timeValue("starts-at", "starts_at"), ends_at: timeValue("ends-at", "ends_at"),
        teacher: $("teacher").value.trim(), location: $("location").value.trim(), note: $("note").value.trim(), status: "scheduled" };
      await api("/courses" + (courseId ? "/" + encodeURIComponent(courseId) : ""), courseId ? "PUT" : "POST", body);
      await loadDetails(); message("课程安排已保存，不会立即发送；将在后续通知时段汇总。" );
    });
  });

  $("course-rows").addEventListener("click", (event) => {
    if (busy) return;
    const edit = event.target.closest("[data-edit]");
    if (edit) {
      const course = courses.find((item) => item.id === edit.dataset.edit);
      if (!course) return;
      $("course-id").value = course.id; $("course-title").value = course.title;
      $("starts-at").value = course.starts_at.slice(0, 16); $("ends-at").value = course.ends_at.slice(0, 16);
      [...$("students").options].forEach((option) => { option.selected = course.student_ids.includes(option.value); });
      $("teacher").value = course.teacher; $("location").value = course.location; $("note").value = course.note;
      $("course-form-title").textContent = "编辑课程安排"; $("save-course").textContent = course.status === "cancelled" ? "恢复并保存课程" : "保存课程修改";
      $("course-form").scrollIntoView({ behavior: "smooth", block: "center" }); $("course-title").focus();
      return;
    }
    const cancel = event.target.closest("[data-cancel]");
    if (cancel && !cancel.disabled && confirm("确认取消该课程？取消不会立即发送，后续通知会标记已取消。紧急变动请另行联系家长。")) run(async () => {
      await api("/courses/" + encodeURIComponent(cancel.dataset.cancel), "DELETE"); await loadDetails(); message("已取消课程，将在后续时段标记通知，不会立即发送。" );
    });
  });

  window.addEventListener("storage", (event) => { if (event.key === "edu_auth") gate(); });
  async function init() {
    $("login-link").href = "/app/login/?next=" + encodeURIComponent("/app/wechat_robot/");
    const current = auth(); ownerToken = current?.token || "";
    if (!ownerToken) { gate(); return; }
    await run(async () => {
      const [students] = await Promise.all([api("/students"), loadRobots()]);
      $("students").innerHTML = students.students.map((student) => '<option value="' + escape(student.id) + '">' + escape(student.display_name || student.username) + '</option>').join("");
      selectedId = robots[0]?.id || ""; robotOptions(); fillRobot(robots[0]);
      $("main").hidden = false; $("gate").hidden = true; await loadDetails();
    });
  }
  $("retry-load").addEventListener("click", init);
  init();
})();
