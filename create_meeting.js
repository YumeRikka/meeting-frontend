// 腾讯会议 - 创建会议（Node.js，无第三方依赖）
// ============================================================
// 安全说明：
//   - 本文件不含任何真实密钥，全部从同目录 .env 读取。
//   - .env 只存在于你本机，不要提交、不要截图、不要发到群里。
//   - 你刚才在聊天里贴过的 SecretKey 已暴露，建议回后台重置（企业管理→高级→REST API→对应应用→重置密钥）。
//
// 运行步骤：
//   1) 把 .env.example 复制为 .env，填入你的真实凭证与参数
//   2) 到腾讯会议后台把"全局白名单"配成【运行本脚本那台机器的公网 IP】
//   3) node create_meeting.js
// ============================================================

const crypto = require('crypto');
const https = require('https');
const fs = require('fs');
const path = require('path');

// 极简读取 .env（避免额外依赖）
function loadEnv() {
  const envPath = path.join(__dirname, '.env');
  if (!fs.existsSync(envPath)) return;
  const text = fs.readFileSync(envPath, 'utf-8');
  for (const line of text.split('\n')) {
    if (!line.trim() || line.trim().startsWith('#')) continue;
    const m = line.match(/^([\w.-]+)=(.*)$/);
    if (m) process.env[m[1]] = m[2].replace(/^["']|["']$/g, '');
  }
}
loadEnv();

const APPID = process.env.APPID;
const SDKID = process.env.SDKID;
const SECRET_ID = process.env.SECRET_ID;
const SECRET_KEY = process.env.SECRET_KEY;
const SUBJECT = process.env.MEETING_SUBJECT || '临时会议';
const HOST_USERID = process.env.MEETING_USERID;
const DURATION_MIN = parseInt(process.env.MEETING_DURATION || '30', 10);
const startTime = parseInt(
  process.env.MEETING_START_TIME || String(Math.floor(Date.now() / 1000) + 120),
  10
);
const endTime = startTime + DURATION_MIN * 60;

// ===== 腾讯会议 TC3-HMAC-SHA256 签名（官方口径：HMAC-SHA256 → hex → Base64）=====
function sign(secretId, secretKey, httpMethod, nonce, timestamp, uri, body) {
  const headerString = `X-TC-Key=${secretId}&X-TC-Nonce=${nonce}&X-TC-Timestamp=${timestamp}`;
  const stringToSign = `${httpMethod}\n${headerString}\n${uri}\n${body}`;
  const hexHash = crypto.createHmac('sha256', secretKey).update(stringToSign, 'utf-8').digest('hex');
  return Buffer.from(hexHash, 'utf-8').toString('base64');
}

function createMeeting() {
  if (!APPID || !SDKID || !SECRET_ID || !SECRET_KEY) {
    console.error('❌ 缺少凭证：请在 .env 中填写 APPID / SDKID / SECRET_ID / SECRET_KEY');
    process.exit(1);
  }
  if (!HOST_USERID) {
    console.error('❌ 缺少 MEETING_USERID：填一个你企业账号下真实存在的用户ID（会议主持人）');
    process.exit(1);
  }

  const uri = '/v1/meetings';
  const timestamp = String(Math.floor(Date.now() / 1000));
  const nonce = String(Math.floor(Math.random() * 1000000000));

  const bodyObj = {
    userid: HOST_USERID,
    instanceid: 1,
    subject: SUBJECT,
    type: 0, // 0=预约会议 1=快速会议
    start_time: startTime,
    end_time: endTime,
    settings: {}
  };
  const bodyStr = JSON.stringify(bodyObj);

  const signature = sign(SECRET_ID, SECRET_KEY, 'POST', nonce, timestamp, uri, bodyStr);

  const headers = {
    'Content-Type': 'application/json',
    'X-TC-Key': SECRET_ID,
    'X-TC-Timestamp': timestamp,
    'X-TC-Nonce': nonce,
    'X-TC-Signature': signature,
    'X-TC-Registered': '1', // 会议进入通讯录；若报通讯录相关错误，可改为 '0' 或删掉该头
    'AppId': APPID,
    'SdkId': SDKID
  };

  const options = {
    hostname: 'api.meeting.qq.com',
    path: uri,
    method: 'POST',
    headers: Object.assign({ 'Content-Length': Buffer.byteLength(bodyStr) }, headers)
  };

  const req = https.request(options, (res) => {
    let chunk = '';
    res.on('data', (d) => (chunk += d));
    res.on('end', () => {
      console.log('HTTP', res.statusCode);
      console.log(chunk);
      try {
        const json = JSON.parse(chunk);
        const list = json.meeting_info_list;
        if (res.statusCode === 200 && list && list[0]) {
          const m = list[0];
          console.log('\n✅ 会议创建成功');
          console.log('会议主题 :', m.subject);
          console.log('会议号   :', m.meeting_code);
          console.log('入会链接 :', m.join_url);
        }
      } catch (e) {
        /* 原始响应已打印 */
      }
    });
  });

  req.on('error', (e) => console.error('请求失败:', e.message));
  req.write(bodyStr);
  req.end();
}

createMeeting();
