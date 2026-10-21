/**
 * Cloudflare Worker: trend-outage-watch
 *
 * 职责:
 * 1. 每小时通过 Cron Trigger 运行一次，抓取看板的 last_run.json
 * 2. full_at > 8 小时 或 fast_at > 2 小时 -> 发送 Telegram 报警
 * 3. 同一种报警 6 小时内最多发一次 (支持 KV 存储或内存降级)
 * 4. 恢复正常后发送一条 "已恢复" 通知
 * 5. 凭据从 Worker Secrets 读取 (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
 */

const DEFAULT_DASHBOARD_URL = "https://trend-dash.pages.dev/data/last_run.json";
const DEFAULT_FULL_THRESHOLD_HOURS = 8.0;
const DEFAULT_FAST_THRESHOLD_HOURS = 2.0;
const DEFAULT_SUPPRESSION_HOURS = 6.0;

// 内存降级存储 (当未绑定 KV 时生效)
let inMemoryState = {
  full: { in_outage: false, last_alert_ts: 0 },
  fast: { in_outage: false, last_alert_ts: 0 },
};

async function getState(env) {
  if (env.OUTAGE_KV) {
    try {
      const data = await env.OUTAGE_KV.get("outage_state", "json");
      if (data && typeof data === "object") {
        return {
          full: data.full || { in_outage: false, last_alert_ts: 0 },
          fast: data.fast || { in_outage: false, last_alert_ts: 0 },
        };
      }
    } catch (e) {
      console.error("[OutageWatch] KV 读取失败:", e);
    }
  }
  return inMemoryState;
}

async function saveState(env, state) {
  inMemoryState = state;
  if (env.OUTAGE_KV) {
    try {
      await env.OUTAGE_KV.put("outage_state", JSON.stringify(state));
    } catch (e) {
      console.error("[OutageWatch] KV 写入失败:", e);
    }
  }
}

async function sendTelegram(env, text) {
  const token = env.TELEGRAM_BOT_TOKEN;
  const chatId = env.TELEGRAM_CHAT_ID;

  if (!token || !chatId) {
    console.warn("[OutageWatch] 未配置 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID，跳过发送");
    return false;
  }

  const url = `https://api.telegram.org/bot${token}/sendMessage`;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text: text,
        disable_web_page_preview: true,
      }),
    });
    if (!res.ok) {
      const err = await res.text();
      console.error("[OutageWatch] Telegram API 错误:", res.status, err);
      return false;
    }
    return true;
  } catch (e) {
    console.error("[OutageWatch] Telegram 发送异常:", e);
    return false;
  }
}

export async function checkOutage(env) {
  const dashboardUrl = env.DASHBOARD_URL || DEFAULT_DASHBOARD_URL;
  const fullThreshold = parseFloat(env.FULL_THRESHOLD_HOURS || DEFAULT_FULL_THRESHOLD_HOURS);
  const fastThreshold = parseFloat(env.FAST_THRESHOLD_HOURS || DEFAULT_FAST_THRESHOLD_HOURS);
  const suppressionHours = parseFloat(env.SUPPRESSION_HOURS || DEFAULT_SUPPRESSION_HOURS);

  let lastRun = null;
  try {
    const resp = await fetch(dashboardUrl, {
      cf: { cacheTtl: 0 }, // 禁用 CF 缓存确保获取最新状态
      headers: { "User-Agent": "trend-outage-watch/1.0" },
    });
    if (resp.ok) {
      lastRun = await resp.json();
    } else {
      console.warn(`[OutageWatch] 获取 last_run.json 返回 HTTP ${resp.status}`);
    }
  } catch (e) {
    console.error("[OutageWatch] 请求 last_run.json 异常:", e);
  }

  const now = Date.now();
  const state = await getState(env);
  const notifications = [];

  const targets = [
    { key: "full", name: "全量任务", threshold: fullThreshold },
    { key: "fast", name: "Fast 任务", threshold: fastThreshold },
  ];

  for (const t of targets) {
    const tsStr = lastRun ? lastRun[`${t.key}_at`] : null;
    let ageHours = null;
    if (tsStr) {
      const parsed = Date.parse(tsStr);
      if (!isNaN(parsed)) {
        ageHours = Math.max(0, (now - parsed) / (3600 * 1000));
      }
    }

    const isOverdue = ageHours === null || ageHours > t.threshold;
    const curr = state[t.key] || { in_outage: false, last_alert_ts: 0 };

    if (isOverdue) {
      curr.in_outage = true;
      const lastAlert = curr.last_alert_ts || 0;
      const timeSinceAlert = (now - lastAlert) / (3600 * 1000);

      if (lastAlert === 0 || timeSinceAlert >= suppressionHours) {
        curr.last_alert_ts = now;
        const ageDesc = ageHours !== null ? `${ageHours.toFixed(1)} 小时` : "无有效时间记录";
        const msg = `⚠️ [Trend Monitor 断档报警] ${t.name}已超过 ${ageDesc} 未运行 (阈值 ${t.threshold} 小时)\n上次运行: ${tsStr || "从未运行"}\n看板地址: ${dashboardUrl}`;
        await sendTelegram(env, msg);
        notifications.push({ type: "alert", target: t.key, message: msg });
      }
    } else {
      if (curr.in_outage) {
        curr.in_outage = false;
        curr.last_alert_ts = 0;
        const msg = `✅ [Trend Monitor 已恢复] ${t.name}已恢复正常运行\n恢复时间: ${tsStr || "刚刚"}`;
        await sendTelegram(env, msg);
        notifications.push({ type: "recovery", target: t.key, message: msg });
      }
    }
    state[t.key] = curr;
  }

  await saveState(env, state);

  return {
    timestamp: new Date(now).toISOString(),
    last_run: lastRun,
    state,
    notifications,
  };
}

export default {
  // Cron Trigger 定时调度入口
  async scheduled(event, env, ctx) {
    ctx.waitUntil(checkOutage(env));
  },

  // HTTP 请求入口 (便于调试和健康检查)
  async fetch(request, env, ctx) {
    const result = await checkOutage(env);
    return new Response(JSON.stringify(result, null, 2), {
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  },
};
