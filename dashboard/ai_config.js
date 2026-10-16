/* ============================================================
   AI 五维投资评价 · 模型目录（v1.15.0）
   ------------------------------------------------------------
   ⚠️ v1.15.0 起 Key 不再写进本文件（仓库公开，写进来等于泄露）。
   流程改为：首次访问看板弹出「AI 配置向导」→ 使用者自己选模型、
   填自己的 Key → 点「测试连通」通过后，保存在**他自己浏览器**
   的 localStorage（键 wb.ai.cfg），本站服务器不经手、不存档。

   下面只是「可选模型清单」，都是 OpenAI 兼容的 /chat/completions。
   想加厂商，往 presets 里添一行即可（id/name/baseUrl/model）。
   ============================================================ */
window.AI_CFG = {
  presets: [
    { id: 'deepseek', name: 'DeepSeek（推荐 · 便宜）',
      baseUrl: 'https://api.deepseek.com/chat/completions', model: 'deepseek-chat' },
    { id: 'zhipu', name: '智谱 GLM',
      baseUrl: 'https://open.bigmodel.cn/api/paas/v4/chat/completions', model: 'glm-4-flash' },
    { id: 'moonshot', name: '月之暗面 Kimi',
      baseUrl: 'https://api.moonshot.cn/v1/chat/completions', model: 'moonshot-v1-8k' },
    { id: 'qwen', name: '通义千问',
      baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions', model: 'qwen-plus' },
    { id: 'custom', name: '自定义（任何 OpenAI 兼容接口）',
      baseUrl: '', model: '' },
  ],

  /* —— 站长体验 Key（v1.16.0）——
     拆成两段 base64 藏在这里，避免整串明文直接暴露在源码里被爬虫扫走。
     访客点向导里的「申请测试 Key」→ 拼回原文、隐匿填入 password 输入框，
     trialTtlMs 毫秒后由页面自动失效（过期会清配置并重新弹向导）。
     ⚠️ 前端存储终究能被有心人还原，这里只能放站长自己的小额体验 Key。 */
  trialKeyParts: ['c2stZjFhNDA1N2VjNmRhNDAx', 'ZjlkMWM5NTQyMDIxM2U3YmY='],
  trialTtlMs: 10 * 60 * 1000,      // 10 分钟
};
