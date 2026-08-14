// 前端后端地址配置
// 逻辑：
//   - 本地开发（localhost / 127.0.0.1 访问）自动指向 http://localhost:5000
//   - 生产（GitHub Pages 等独立前端）由 CI 把本文件替换为 config.prod.js 的内容，
//     即指向你的后端域名（https://api.yourdomain.com）。
// 如需手动覆盖，可在本文件直接改 window.API_BASE 的值。
(function () {
  var h = location.hostname;
  if (h === "localhost" || h === "127.0.0.1") {
    window.API_BASE = "http://localhost:5000";
  } else {
    // 生产环境占位（CI 发布时会被 config.prod.js 覆盖）
    window.API_BASE = "https://api.rikka.com.cn";
  }
})();
