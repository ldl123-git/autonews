# Fix Adapter

修复适配器同步问题。

## 需要的信息

请提供：
1. **平台名称**: 如 `zhihu`, `juejin`
2. **错误信息**: 控制台错误或同步失败提示
3. **预期行为**: 应该发生什么

## 调试步骤

1. 读取对应适配器文件 `packages/core/src/adapters/platforms/{platform}.ts`
2. 分析 `checkAuth()` 和 `publish()` 方法
3. 检查：
   - API 端点是否正确
   - Header 规则是否需要更新
   - CSRF token 提取是否正确
   - 响应解析是否正确
4. 提出修复方案

## 常见问题

- **401/403 错误**: 检查 Cookie 和 CSRF token
- **CORS 错误**: 检查 Header 规则配置
- **解析失败**: API 响应格式可能已变化
