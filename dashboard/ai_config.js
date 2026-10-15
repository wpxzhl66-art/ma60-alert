/* ============================================================
   AI 五维投资评价 · 配置（v1.13.0）
   ------------------------------------------------------------
   模型选型（我定的）：DeepSeek deepseek-chat
     —— OpenAI 兼容接口、中文财经语料理解好、按量计费便宜
        （评价一只股票约 1 千字输出，成本不到 1 分钱）。

   想换其他厂商？只要是「OpenAI 兼容的 /chat/completions」都行，
   改 baseUrl + model 两个值即可，例如：
     智谱 GLM:  baseUrl 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
                model   'glm-4-flash'
     月之暗面:  baseUrl 'https://api.moonshot.cn/v1/chat/completions'
                model   'moonshot-v1-8k'
     通义千问:  baseUrl 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
                model   'qwen-plus'

   ⚠️ apiKey 留空 = 功能显示「未配置」，其他功能完全不受影响。
      申请地址（DeepSeek）：https://platform.deepseek.com
   ============================================================ */
window.AI_CFG = {
  baseUrl: 'https://api.deepseek.com/chat/completions',
  model: 'deepseek-chat',
  apiKey: '',   // ←←← 把你的 API Key 填到这两个引号之间
};
