/**
 * Cloudflare Worker - Bot hỏi đáp chứng khoán qua Telegram.
 *
 * Gửi tin nhắn chữ hoặc ảnh (chụp bài báo, biểu đồ...) cho bot -> Claude đọc, đối chiếu bối cảnh thị trường
 * (bản tin 21:00 gần nhất + giá hiện tại của danh mục) và trả lời nhận định.
 *
 * Biến môi trường (Settings -> Variables and Secrets):
 *   TELEGRAM_BOT_TOKEN  (Secret)  token bot Telegram
 *   ANTHROPIC_API_KEY   (Secret)  khoá Claude API
 *   CHAT_ID             (Text)    chỉ trả lời chat này, người lạ nhắn sẽ bị bỏ qua
 *   AI_MODEL            (Text, tuỳ chọn) mặc định claude-opus-5-5
 *
 * Sau khi deploy, mở https://<tên-worker>.<tài-khoản>.workers.dev/setup một lần để đăng ký webhook với Telegram.
 */

const CONTEXT_URL = "https://raw.githubusercontent.com/hoanghung11090-jpg/stock-bot/main/latest_context.json";
const WATCHLIST = ["VNINDEX", "VPB", "VPX", "TCB", "MBB", "SSI"];

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const hookPath = "/hook/" + (await sha256(env.TELEGRAM_BOT_TOKEN || "")).slice(0, 24);

    // Đăng ký webhook: đường dẫn bí mật suy ra từ token nên người ngoài không đoán được.
    if (url.pathname === "/setup") {
      const r = await tg(env, "setWebhook", {
        url: `${url.origin}${hookPath}`,
        allowed_updates: ["message"],
        drop_pending_updates: true,
      });
      return new Response(JSON.stringify(r), { headers: { "content-type": "application/json" } });
    }

    if (request.method === "POST" && url.pathname === hookPath) {
      const update = await request.json().catch(() => null);
      const msg = update && update.message;
      // Chỉ phục vụ đúng chủ bot
      if (msg && String(msg.chat.id) === String(env.CHAT_ID)) {
        ctx.waitUntil(handle(env, msg).catch((e) => reply(env, msg.chat.id, "Lỗi xử lý: " + e.message)));
      }
      return new Response("ok"); // trả lời Telegram ngay, xử lý chạy nền
    }
    return new Response("Stock bot worker is running.");
  },
};

async function handle(env, msg) {
  const chatId = msg.chat.id;
  const text = (msg.text || msg.caption || "").trim();
  if (text === "/start" || text === "/help") {
    return reply(env, chatId,
      "Gửi cho bot: tin tức, ảnh chụp bài báo/biểu đồ, hoặc câu hỏi (vd: 'MBB phát hành riêng lẻ ảnh hưởng gì?').\n" +
      "Bot sẽ nhận định dựa trên tin đó + bối cảnh thị trường mới nhất. Chỉ để tham khảo, không phải khuyến nghị đầu tư.");
  }
  await tg(env, "sendChatAction", { chat_id: chatId, action: "typing" });

  const content = [];
  if (msg.photo && msg.photo.length) {
    // Chọn ảnh cỡ vừa (đủ đọc chữ, nhẹ hơn ảnh gốc)
    const photo = msg.photo[Math.max(0, msg.photo.length - 2)];
    const img = await telegramFileBase64(env, photo.file_id);
    if (img) content.push({ type: "image", source: { type: "base64", media_type: img.type, data: img.data } });
  }

  const [context, quotes] = await Promise.all([loadContext(), liveQuotes()]);
  content.push({
    type: "text",
    text:
      `Bối cảnh thị trường (bản tin gần nhất, JSON): ${JSON.stringify(context)}\n` +
      `Giá hiện tại danh mục (JSON, giá nghìn đồng): ${JSON.stringify(quotes)}\n\n` +
      `Tin/câu hỏi của nhà đầu tư: ${text || "(xem ảnh đính kèm)"}\n\n` +
      "Trả lời bằng tiếng Việt, ngắn gọn, không markdown, tối đa ~12 dòng, theo cấu trúc:\n" +
      "Tóm tắt: nội dung chính của tin (1-2 câu).\n" +
      "Tác động: tới thị trường chung và từng mã liên quan trong danh mục VPB, VPX, TCB, MBB, SSI (tích cực/tiêu cực/trung tính, vì sao).\n" +
      "Mức độ: ngắn hạn hay dài hạn, mạnh hay yếu.\n" +
      "Cần theo dõi: 1-2 dấu hiệu xác nhận hoặc phủ định.\n" +
      "Quy tắc: phân biệt rõ dữ kiện trong tin và nhận định của bạn; không bịa số liệu; nếu không chắc thì nói không chắc; " +
      "không khuyến nghị mua/bán cụ thể.",
  });

  const answer = await askClaude(env, content);
  await reply(env, chatId, answer + "\n\n— Nhận định tham khảo, không phải khuyến nghị đầu tư.");
}

async function askClaude(env, content) {
  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "x-api-key": env.ANTHROPIC_API_KEY,
      "anthropic-version": "2023-06-01",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      model: env.AI_MODEL || "claude-opus-5-5",
      max_tokens: 1200,
      system: "Bạn là chuyên viên phân tích chứng khoán Việt Nam, trả lời thẳng thắn, có căn cứ, thận trọng.",
      messages: [{ role: "user", content }],
    }),
  });
  if (!r.ok) throw new Error(`Claude API ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const j = await r.json();
  return j.content.map((b) => b.text || "").join("").trim();
}

async function loadContext() {
  try {
    const r = await fetch(CONTEXT_URL, { cf: { cacheTtl: 300 } });
    return r.ok ? await r.json() : null;
  } catch (e) {
    return null;
  }
}

async function liveQuotes() {
  // Giá gần nhất + % ngày từ VNDirect (nếu nguồn chặn thì bỏ qua)
  const now = Math.floor(Date.now() / 1000);
  const out = {};
  await Promise.all(WATCHLIST.map(async (s) => {
    try {
      const r = await fetch(`https://dchart-api.vndirect.com.vn/dchart/history?resolution=D&symbol=${s}&from=${now - 864000}&to=${now}`,
        { headers: { "User-Agent": "Mozilla/5.0" } });
      const d = await r.json();
      if (d.s === "ok" && d.c.length > 1) {
        const c = d.c, n = c.length;
        out[s] = { price: c[n - 1], pct: +((c[n - 1] / c[n - 2] - 1) * 100).toFixed(2) };
      }
    } catch (e) { /* bỏ qua */ }
  }));
  return out;
}

async function telegramFileBase64(env, fileId) {
  const f = await tg(env, "getFile", { file_id: fileId });
  if (!f.ok) return null;
  const r = await fetch(`https://api.telegram.org/file/bot${env.TELEGRAM_BOT_TOKEN}/${f.result.file_path}`);
  const buf = new Uint8Array(await r.arrayBuffer());
  let bin = "";
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  const ext = f.result.file_path.split(".").pop().toLowerCase();
  const type = ext === "png" ? "image/png" : ext === "webp" ? "image/webp" : "image/jpeg";
  return { type, data: btoa(bin) };
}

async function reply(env, chatId, text) {
  // Telegram giới hạn 4096 ký tự/tin
  for (let i = 0; i < text.length; i += 4000) {
    await tg(env, "sendMessage", { chat_id: chatId, text: text.slice(i, i + 4000), disable_web_page_preview: true });
  }
}

async function tg(env, method, body) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

async function sha256(s) {
  const h = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
