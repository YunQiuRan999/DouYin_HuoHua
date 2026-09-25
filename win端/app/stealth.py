"""反自动化指纹：降低代管账号被抖音风控识别的概率。

原理：在页面加载前（add_init_script）抹平 Playwright / Chromium 常见的自动化
特征，包括 webdriver 标志、UA 一致性、插件列表、WebGL 渲染厂商、权限接口等。
只做特征一致性处理，不伪造或注入任何真实用户数据。
"""
from __future__ import annotations

STEALTH_SCRIPT = r"""
(() => {
  const nav = navigator;

  // 1. 抹平 webdriver 自动化标志（最常见的检测点）
  try {
    Object.defineProperty(nav, 'webdriver', { get: () => undefined });
  } catch (_) {}

  // 2. 补齐 chrome 运行时对象（部分检测依赖其存在）
  if (!window.chrome) {
    try {
      window.chrome = { runtime: {} };
    } catch (_) {}
  }
  try {
    const makeObj = () => new Object();
    window.chrome.csi = window.chrome.csi || makeObj;
    window.chrome.loadTimes = window.chrome.loadTimes || makeObj;
  } catch (_) {}

  // 3. 固定语言列表与平台，避免与实际环境不一致
  try {
    Object.defineProperty(nav, 'languages', {
      get: () => ['zh-CN', 'zh', 'en'],
    });
  } catch (_) {}

  // 4. 补齐插件列表（无头环境常为空，与真实浏览器不一致）
  if (nav.plugins && nav.plugins.length === 0) {
    try {
      Object.defineProperty(nav, 'plugins', {
        get: () => [1, 2, 3, 4, 5].map(() => ({ length: 0, namedItem: () => null, item: () => null })),
      });
    } catch (_) {}
  }

  // 5. 抹平 WebGL 软件渲染厂商特征（SwiftShader / Google 等常见标记）
  try {
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (param) {
      if (param === 37445) return 'Intel Inc.';
      if (param === 37446) return 'Intel Iris OpenGL Engine';
      return getParam.call(this, param);
    };
  } catch (_) {}

  // 6. 权限查询接口对齐（notification 显示为 denied，避免自动化典型值）
  if (nav.permissions && nav.permissions.query) {
    const query = nav.permissions.query.bind(nav.permissions);
    nav.permissions.query = (descriptor) =>
      descriptor && descriptor.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : query(descriptor);
  }

  // 7. UA 一致性：无头模式的 HeadlessChrome 字样会暴露自动化，抹平为普通 Chrome
  try {
    const rawUa = nav.userAgent;
    const fixedUa = rawUa.replace('HeadlessChrome', 'Chrome').replace('Headless', '');
    if (fixedUa !== rawUa) {
      Object.defineProperty(nav, 'userAgent', { get: () => fixedUa });
    }
  } catch (_) {}

  // 8. 硬件指纹：统一核心数与内存，避免无头环境暴露异常小值
  try {
    Object.defineProperty(nav, 'hardwareConcurrency', { get: () => 8 });
  } catch (_) {}
  try {
    Object.defineProperty(nav, 'deviceMemory', { get: () => 8 });
  } catch (_) {}
  try {
    Object.defineProperty(nav, 'maxTouchPoints', { get: () => 0 });
  } catch (_) {}

  // 9. Canvas 指纹噪声：对 2D 像素做 ±1 的不可见扰动，
  //    破坏自动化环境与真人环境指纹的精确匹配（风控常用指纹比对）
  try {
    const originalGetContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (type, ...args) {
      const ctx = originalGetContext.call(this, type, ...args);
      if (type === '2d' && ctx && !ctx.__douyinNoised) {
        Object.defineProperty(ctx, '__douyinNoised', { value: true });
        const originalGetImageData = ctx.getImageData;
        ctx.getImageData = function (x, y, w, h) {
          const imageData = originalGetImageData.call(this, x, y, w, h);
          const d = imageData.data;
          if (d && d.length > 4) {
            for (let i = 3; i < d.length; i += 4) {
              d[i] = (d[i] ^ 0x02) >>> 0;  // 轻微 alpha 扰动，肉眼不可见
            }
          }
          return imageData;
        };
      }
      return ctx;
    };
  } catch (_) {}
})();
"""
