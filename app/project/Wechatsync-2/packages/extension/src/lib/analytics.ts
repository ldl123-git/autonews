/**
 * Google Analytics 4 追踪模块
 * 使用 Measurement Protocol 在 Service Worker 中发送事件
 */
import { createLogger } from './logger'

const logger = createLogger('Analytics')

// GA4 配置 - 通过环境变量配置
const GA_MEASUREMENT_ID = import.meta.env.VITE_GA_MEASUREMENT_ID || ''
const GA_API_SECRET = import.meta.env.VITE_GA_API_SECRET || ''

// Measurement Protocol 端点
const GA_ENDPOINT = `https://www.google-analytics.com/mp/collect?measurement_id=${GA_MEASUREMENT_ID}&api_secret=${GA_API_SECRET}`

// 获取或创建客户端 ID
async function getClientId(): Promise<string> {
  const storage = await chrome.storage.local.get('ga_client_id')
  if (storage.ga_client_id) {
    return storage.ga_client_id
  }

  // 生成新的客户端 ID
  const clientId = crypto.randomUUID()
  await chrome.storage.local.set({ ga_client_id: clientId })
  return clientId
}

// 获取会话 ID
async function getSessionId(): Promise<string> {
  const storage = await chrome.storage.local.get(['ga_session_id', 'ga_session_timestamp'])
  const now = Date.now()
  const SESSION_TIMEOUT = 30 * 60 * 1000 // 30 分钟

  // 检查会话是否过期
  if (storage.ga_session_id && storage.ga_session_timestamp) {
    if (now - storage.ga_session_timestamp < SESSION_TIMEOUT) {
      // 更新会话时间戳
      await chrome.storage.local.set({ ga_session_timestamp: now })
      return storage.ga_session_id
    }
  }

  // 创建新会话
  const sessionId = Date.now().toString()
  await chrome.storage.local.set({
    ga_session_id: sessionId,
    ga_session_timestamp: now,
  })
  return sessionId
}

// 发送事件到 GA4
async function sendEvent(eventName: string, params: Record<string, any> = {}): Promise<void> {
  // 检查是否配置了 GA
  if (!GA_MEASUREMENT_ID || !GA_API_SECRET) {
    return
  }

  // 检查是否启用分析（可选的隐私设置）
  const storage = await chrome.storage.local.get('analytics_enabled')
  if (storage.analytics_enabled === false) {
    return
  }

  try {
    const clientId = await getClientId()
    const sessionId = await getSessionId()

    const payload = {
      client_id: clientId,
      events: [
        {
          name: eventName,
          params: {
            session_id: sessionId,
            engagement_time_msec: 100,
            ...params,
          },
        },
      ],
    }

    // 发送到 GA4
    const response = await fetch(GA_ENDPOINT, {
      method: 'POST',
      body: JSON.stringify(payload),
    })

    if (!response.ok) {
      logger.warn('Failed to send event:', response.status)
    }
  } catch (error) {
    logger.warn('Error sending event:', error)
  }
}

/**
 * 追踪同步开始事件
 * @param source 文章来源（weixin, weixin-editor, popup, mcp 等）
 * @param targetPlatforms 目标同步平台列表
 */
export async function trackSyncStart(
  source: string,
  targetPlatforms: string[]
): Promise<void> {
  await sendEvent('sync_start', {
    source,
    target_count: targetPlatforms.length,
    targets: targetPlatforms.slice(0, 10).join(','), // GA4 参数值最大 100 字符
  })
}

/**
 * 追踪单个平台同步结果（合并了平台健康度数据）
 * @param source 文章来源
 * @param targetPlatform 目标平台
 * @param success 是否成功
 * @param options 其他选项
 */
export async function trackPlatformSync(
  source: string,
  targetPlatform: string,
  success: boolean,
  options: {
    draftOnly?: boolean
    errorType?: string
    duration?: number
  } = {}
): Promise<void> {
  // 响应时间分段（用于平台健康度分析）
  const duration = options.duration || 0
  let responseRange: string
  if (duration < 500) responseRange = '0-500ms'
  else if (duration < 1000) responseRange = '500ms-1s'
  else if (duration < 3000) responseRange = '1-3s'
  else if (duration < 5000) responseRange = '3-5s'
  else responseRange = '5s+'

  await sendEvent('platform_sync', {
    source,
    target: targetPlatform,
    success: success ? 'true' : 'false',
    draft_only: options.draftOnly ? 'true' : 'false',
    error_type: options.errorType || '',
    duration_ms: duration,
    response_range: responseRange, // 合并自 trackPlatformHealth
  })
}

/**
 * 追踪同步完成事件
 * @param results 同步结果汇总
 */
export async function trackSyncComplete(results: {
  source: string
  total: number
  success: number
  failed: number
  platforms: string[]
  duration: number
}): Promise<void> {
  await sendEvent('sync_complete', {
    source: results.source,
    total_platforms: results.total,
    success_count: results.success,
    failed_count: results.failed,
    success_rate: results.total > 0 ? Math.round((results.success / results.total) * 100) : 0,
    targets: results.platforms.slice(0, 10).join(','),
    duration_ms: results.duration,
  })
}

/**
 * 追踪文章提取事件（简化版，详细内容特征用 trackContentProfile）
 * @deprecated 建议使用 trackContentProfile 替代，提供更丰富的内容分析
 */
export async function trackArticleExtract(
  source: string,
  success: boolean,
  _details?: {
    hasTitle?: boolean
    hasContent?: boolean
    hasCover?: boolean
    contentLength?: number
  }
): Promise<void> {
  // 简化为只追踪提取成功/失败，详细特征由 trackContentProfile 处理
  await sendEvent('article_extract', {
    source,
    success: success ? 'true' : 'false',
  })
}

/**
 * 追踪扩展安装/更新
 */
export async function trackInstall(reason: string, previousVersion?: string): Promise<void> {
  await sendEvent('extension_lifecycle', {
    reason,
    previous_version: previousVersion || '',
    current_version: chrome.runtime.getManifest().version,
  })
}

/**
 * 追踪 CMS 同步
 * @param source 文章来源
 * @param cmsType CMS 类型（wordpress, typecho, metaweblog）
 * @param success 是否成功
 */
export async function trackCmsSync(
  source: string,
  cmsType: string,
  success: boolean
): Promise<void> {
  await sendEvent('cms_sync', {
    source,
    cms_type: cmsType,
    success: success ? 'true' : 'false',
  })
}

/**
 * 追踪页面浏览（popup 等）
 */
export async function trackPageView(pageName: string): Promise<void> {
  await sendEvent('page_view', {
    page_name: pageName,
  })
}

/**
 * 追踪功能使用
 */
export async function trackFeatureUse(feature: string, details?: Record<string, any>): Promise<void> {
  await sendEvent('feature_use', {
    feature,
    ...details,
  })
}

/**
 * 追踪平台登录状态检查
 * @param platform 平台 ID
 * @param isLoggedIn 是否已登录
 */
export async function trackAuthCheck(platform: string, isLoggedIn: boolean): Promise<void> {
  await sendEvent('auth_check', {
    platform,
    is_logged_in: isLoggedIn ? 'true' : 'false',
  })
}

/**
 * 追踪图片上传
 * @param source 文章来源
 * @param targetPlatform 目标平台
 * @param imageCount 图片数量
 * @param success 是否全部成功
 */
export async function trackImageUpload(
  source: string,
  targetPlatform: string,
  imageCount: number,
  success: boolean
): Promise<void> {
  await sendEvent('image_upload', {
    source,
    target: targetPlatform,
    image_count: imageCount,
    success: success ? 'true' : 'false',
  })
}

// ============ 新增追踪功能 ============

/**
 * 错误类型枚举（用于错误分类分析）
 */
export type SyncErrorType =
  | 'auth_expired'      // 登录态过期
  | 'rate_limit'        // 频率限制
  | 'network'           // 网络错误
  | 'api_error'         // API 返回错误
  | 'content_blocked'   // 内容被拦截/审核
  | 'image_upload'      // 图片上传失败
  | 'parse_error'       // 解析错误
  | 'unknown'           // 未知错误

/**
 * 从错误消息推断错误类型
 */
export function inferErrorType(error: string): SyncErrorType {
  const lowerError = error.toLowerCase()

  if (lowerError.includes('login') || lowerError.includes('登录') ||
      lowerError.includes('auth') || lowerError.includes('token') ||
      lowerError.includes('session') || lowerError.includes('credential')) {
    return 'auth_expired'
  }

  if (lowerError.includes('rate') || lowerError.includes('limit') ||
      lowerError.includes('频繁') || lowerError.includes('too many') ||
      lowerError.includes('429')) {
    return 'rate_limit'
  }

  if (lowerError.includes('network') || lowerError.includes('fetch') ||
      lowerError.includes('timeout') || lowerError.includes('连接') ||
      lowerError.includes('econnrefused') || lowerError.includes('网络')) {
    return 'network'
  }

  if (lowerError.includes('blocked') || lowerError.includes('审核') ||
      lowerError.includes('违规') || lowerError.includes('敏感') ||
      lowerError.includes('forbidden') || lowerError.includes('reject')) {
    return 'content_blocked'
  }

  if (lowerError.includes('image') || lowerError.includes('图片') ||
      lowerError.includes('upload') || lowerError.includes('上传')) {
    return 'image_upload'
  }

  if (lowerError.includes('parse') || lowerError.includes('json') ||
      lowerError.includes('解析')) {
    return 'parse_error'
  }

  if (lowerError.includes('api') || lowerError.includes('server') ||
      lowerError.includes('500') || lowerError.includes('502') ||
      lowerError.includes('503')) {
    return 'api_error'
  }

  return 'unknown'
}

/**
 * 追踪重试行为
 */
export async function trackRetry(
  source: string,
  platforms: string[],
  attemptNumber: number,
  previousFailCount: number
): Promise<void> {
  await sendEvent('sync_retry', {
    source,
    platforms: platforms.slice(0, 10).join(','),
    platform_count: platforms.length,
    attempt: attemptNumber,
    previous_fail_count: previousFailCount,
  })
}

/**
 * 追踪内容特征
 */
export async function trackContentProfile(profile: {
  source: string
  wordCount: number
  imageCount: number
  hasCode: boolean
  hasCover: boolean
  hasVideo: boolean
}): Promise<void> {
  // 将字数分段（便于 GA4 分析）
  let wordCountRange: string
  if (profile.wordCount < 500) wordCountRange = '0-500'
  else if (profile.wordCount < 1000) wordCountRange = '500-1000'
  else if (profile.wordCount < 2000) wordCountRange = '1000-2000'
  else if (profile.wordCount < 5000) wordCountRange = '2000-5000'
  else wordCountRange = '5000+'

  await sendEvent('content_profile', {
    source: profile.source,
    word_count: profile.wordCount,
    word_count_range: wordCountRange,
    image_count: profile.imageCount,
    has_code: profile.hasCode ? 'true' : 'false',
    has_cover: profile.hasCover ? 'true' : 'false',
    has_video: profile.hasVideo ? 'true' : 'false',
  })
}

/**
 * 追踪面板交互
 */
export async function trackPanelInteraction(
  action: 'open' | 'close' | 'expand' | 'collapse',
  source: string
): Promise<void> {
  await sendEvent('panel_interaction', {
    action,
    source,
  })
}

/**
 * 追踪平台选择行为
 */
export async function trackPlatformSelection(
  action: 'select' | 'deselect' | 'select_all' | 'deselect_all',
  platform: string,
  totalSelected: number
): Promise<void> {
  await sendEvent('platform_selection', {
    action,
    platform,
    total_selected: totalSelected,
  })
}

// trackFirstSync 已合并到 trackMilestone('first_sync_success')
// 保留函数签名以兼容现有调用，内部转发到 milestone
/**
 * @deprecated 使用 trackMilestone('first_sync_success') 替代
 */
export async function trackFirstSync(
  _success: boolean,
  _platformCount: number,
  _daysSinceInstall: number
): Promise<void> {
  // 不再单独追踪，由 checkAndTrackFirstSync 调用 trackMilestone 处理
}

/**
 * 追踪草稿链接点击
 */
export async function trackDraftClick(platform: string): Promise<void> {
  await sendEvent('draft_click', {
    platform,
  })
}

/**
 * 追踪 CMS 账户管理
 */
export async function trackCmsManagement(
  action: 'add' | 'remove' | 'test',
  cmsType: string,
  success: boolean
): Promise<void> {
  await sendEvent('cms_management', {
    action,
    cms_type: cmsType,
    success: success ? 'true' : 'false',
  })
}

/**
 * 追踪 MCP 使用
 */
export async function trackMcpUsage(
  action: 'enable' | 'disable' | 'connect' | 'disconnect' | 'call',
  method?: string
): Promise<void> {
  await sendEvent('mcp_usage', {
    action,
    method: method || '',
  })
}

/**
 * 追踪性能指标
 */
export async function trackPerformance(metrics: {
  type: 'extract' | 'sync' | 'image_upload'
  platform?: string
  duration: number
  success: boolean
}): Promise<void> {
  // 将耗时分段
  let durationRange: string
  if (metrics.duration < 1000) durationRange = '0-1s'
  else if (metrics.duration < 3000) durationRange = '1-3s'
  else if (metrics.duration < 5000) durationRange = '3-5s'
  else if (metrics.duration < 10000) durationRange = '5-10s'
  else if (metrics.duration < 30000) durationRange = '10-30s'
  else durationRange = '30s+'

  await sendEvent('performance', {
    type: metrics.type,
    platform: metrics.platform || '',
    duration_ms: metrics.duration,
    duration_range: durationRange,
    success: metrics.success ? 'true' : 'false',
  })
}

/**
 * 追踪用户漏斗
 */
export async function trackFunnel(
  step: 'panel_open' | 'platform_selected' | 'sync_started' | 'sync_completed',
  source: string,
  details?: Record<string, any>
): Promise<void> {
  await sendEvent('funnel', {
    step,
    source,
    ...details,
  })
}

/**
 * 检查并追踪首次同步（已整合到里程碑系统）
 * @deprecated 首次同步现在通过 trackMilestone('first_sync_success') 自动追踪
 */
export async function checkAndTrackFirstSync(
  _success: boolean,
  _platformCount: number
): Promise<void> {
  // 首次同步追踪已整合到 trackMilestone('first_sync_success')
  // 该函数保留以兼容现有调用，但不再执行任何操作
}

/**
 * 记录安装时间（在 onInstalled 时调用）
 */
export async function recordInstallTimestamp(): Promise<void> {
  const storage = await chrome.storage.local.get('install_timestamp')
  if (!storage.install_timestamp) {
    await chrome.storage.local.set({ install_timestamp: Date.now() })
  }
}

// ============ 高级产品分析追踪 ============

// trackPlatformHealth 已合并到 trackPlatformSync（包含 response_range）
/**
 * @deprecated 已合并到 trackPlatformSync，包含响应时间分段
 */
export async function trackPlatformHealth(_metrics: {
  platform: string
  responseTime: number
  available: boolean
  errorCode?: string
}): Promise<void> {
  // 不再单独追踪，trackPlatformSync 已包含此信息
}

/**
 * 用户激活里程碑类型
 */
export type MilestoneType =
  | 'first_platform_login'  // 首次登录任一平台
  | 'first_sync_attempt'    // 首次尝试同步
  | 'first_sync_success'    // 首次同步成功
  | 'fifth_sync'            // 第5次同步
  | 'tenth_sync'            // 第10次同步
  | 'multi_platform'        // 使用多平台（3+）
  | 'power_user'            // 高级用户（50+次同步）
  | 'cms_user'              // 使用自建站
  | 'mcp_user'              // 使用 MCP

/**
 * 追踪用户激活里程碑
 */
export async function trackMilestone(
  milestone: MilestoneType,
  details?: Record<string, any>
): Promise<void> {
  // 检查是否已追踪过该里程碑
  const storageKey = `milestone_${milestone}`
  const storage = await chrome.storage.local.get(storageKey)
  if (storage[storageKey]) {
    return // 已追踪过
  }

  // 计算安装后天数
  const installStorage = await chrome.storage.local.get('install_timestamp')
  const daysSinceInstall = installStorage.install_timestamp
    ? Math.floor((Date.now() - installStorage.install_timestamp) / (24 * 60 * 60 * 1000))
    : 0

  await sendEvent('milestone', {
    milestone,
    days_since_install: daysSinceInstall,
    ...details,
  })

  // 标记已追踪
  await chrome.storage.local.set({ [storageKey]: Date.now() })
}

/**
 * 追踪用户流失预警信号
 */
export async function trackChurnSignal(
  signal: 'auth_expired_ignored' | 'multiple_failures' | 'sync_cancelled' | 'uninstall_intent',
  context?: Record<string, any>
): Promise<void> {
  await sendEvent('churn_signal', {
    signal,
    ...context,
  })
}

/**
 * 追踪平台组合（哪些平台经常一起使用）
 */
export async function trackPlatformCombination(platforms: string[]): Promise<void> {
  if (platforms.length < 2) return

  // 排序以确保相同组合产生相同的键
  const sortedPlatforms = [...platforms].sort()
  const combinationKey = sortedPlatforms.slice(0, 5).join('+') // 最多取5个

  await sendEvent('platform_combination', {
    combination: combinationKey,
    platform_count: platforms.length,
    platforms: sortedPlatforms.slice(0, 10).join(','),
  })
}

/**
 * 追踪使用时段
 */
export async function trackUsageTime(): Promise<void> {
  const now = new Date()
  const hour = now.getHours()
  const dayOfWeek = now.getDay() // 0=周日, 1=周一, ...

  // 时段分类
  let timeSlot: string
  if (hour >= 6 && hour < 9) timeSlot = 'early_morning'
  else if (hour >= 9 && hour < 12) timeSlot = 'morning'
  else if (hour >= 12 && hour < 14) timeSlot = 'noon'
  else if (hour >= 14 && hour < 18) timeSlot = 'afternoon'
  else if (hour >= 18 && hour < 22) timeSlot = 'evening'
  else timeSlot = 'night'

  // 工作日/周末
  const isWeekend = dayOfWeek === 0 || dayOfWeek === 6

  await sendEvent('usage_time', {
    hour,
    day_of_week: dayOfWeek,
    time_slot: timeSlot,
    is_weekend: isWeekend ? 'true' : 'false',
  })
}

// trackSyncFlow 已合并到 trackSyncStart（都包含 source 和 targets）
/**
 * @deprecated 使用 trackSyncStart 替代，已包含来源和目标信息
 */
export async function trackSyncFlow(
  _sourcePlatform: string,
  _targetPlatforms: string[]
): Promise<void> {
  // 不再单独追踪，trackSyncStart 已包含此信息
}

/**
 * 追踪功能发现路径
 */
export async function trackFeatureDiscovery(
  feature: string,
  entryPoint: string
): Promise<void> {
  await sendEvent('feature_discovery', {
    feature,
    entry_point: entryPoint,
  })
}

/**
 * 追踪隐式反馈（用户行为信号）
 */
export async function trackImplicitFeedback(
  type: 'quick_close' | 'cancel_mid_sync' | 'long_dwell' | 'immediate_retry' | 'abandon_after_error',
  context?: Record<string, any>
): Promise<void> {
  await sendEvent('implicit_feedback', {
    type,
    ...context,
  })
}

/**
 * 追踪帮助寻求行为
 */
export async function trackHelpSeeking(
  action: 'faq_click' | 'support_link' | 'github_issue' | 'feedback_click'
): Promise<void> {
  await sendEvent('help_seeking', {
    action,
  })
}

/**
 * 追踪用户增长指标
 */
export async function trackGrowthMetrics(): Promise<void> {
  const storage = await chrome.storage.local.get([
    'total_syncs',
    'total_articles',
    'platforms_used',
    'install_timestamp',
  ])

  const totalSyncs = storage.total_syncs || 0
  const totalArticles = storage.total_articles || 0
  const platformsUsed = storage.platforms_used || []
  const daysSinceInstall = storage.install_timestamp
    ? Math.floor((Date.now() - storage.install_timestamp) / (24 * 60 * 60 * 1000))
    : 0

  // 计算用户等级
  let userTier: string
  if (totalSyncs >= 100) userTier = 'power'
  else if (totalSyncs >= 50) userTier = 'active'
  else if (totalSyncs >= 10) userTier = 'engaged'
  else if (totalSyncs >= 1) userTier = 'activated'
  else userTier = 'new'

  await sendEvent('growth_metrics', {
    total_syncs: totalSyncs,
    total_articles: totalArticles,
    platforms_count: platformsUsed.length,
    days_since_install: daysSinceInstall,
    user_tier: userTier,
    syncs_per_day: daysSinceInstall > 0 ? Math.round(totalSyncs / daysSinceInstall * 10) / 10 : 0,
  })
}

/**
 * 更新累计统计数据
 */
export async function updateCumulativeStats(platforms: string[]): Promise<void> {
  const storage = await chrome.storage.local.get(['total_syncs', 'platforms_used'])

  const totalSyncs = (storage.total_syncs || 0) + 1
  const platformsUsed = new Set(storage.platforms_used || [])
  platforms.forEach(p => platformsUsed.add(p))

  await chrome.storage.local.set({
    total_syncs: totalSyncs,
    platforms_used: Array.from(platformsUsed),
  })

  // 检查里程碑
  if (totalSyncs === 5) {
    trackMilestone('fifth_sync').catch(() => {})
  } else if (totalSyncs === 10) {
    trackMilestone('tenth_sync').catch(() => {})
  } else if (totalSyncs === 50) {
    trackMilestone('power_user').catch(() => {})
  }

  if (platformsUsed.size >= 3) {
    trackMilestone('multi_platform', { platform_count: platformsUsed.size }).catch(() => {})
  }
}

/**
 * 追踪会话深度（单次会话中的操作数）
 */
export async function trackSessionDepth(actionCount: number): Promise<void> {
  let depthLevel: string
  if (actionCount <= 1) depthLevel = 'shallow'
  else if (actionCount <= 3) depthLevel = 'normal'
  else if (actionCount <= 5) depthLevel = 'engaged'
  else depthLevel = 'deep'

  await sendEvent('session_depth', {
    action_count: actionCount,
    depth_level: depthLevel,
  })
}

/**
 * 追踪平台扩展（用户登录新平台）
 */
export async function trackPlatformExpansion(
  newPlatform: string,
  totalPlatforms: number
): Promise<void> {
  await sendEvent('platform_expansion', {
    new_platform: newPlatform,
    total_platforms: totalPlatforms,
  })
}

export default {
  // 基础同步追踪
  trackSyncStart,
  trackPlatformSync,
  trackSyncComplete,
  trackArticleExtract,
  trackInstall,
  trackCmsSync,
  trackPageView,
  trackFeatureUse,
  trackAuthCheck,
  trackImageUpload,
  // 错误分析
  inferErrorType,
  trackRetry,
  // 内容与交互
  trackContentProfile,
  trackPanelInteraction,
  trackPlatformSelection,
  trackDraftClick,
  // 首次体验
  trackFirstSync,
  checkAndTrackFirstSync,
  recordInstallTimestamp,
  // 功能采用
  trackCmsManagement,
  trackMcpUsage,
  // 性能与漏斗
  trackPerformance,
  trackFunnel,
  // 高级产品分析
  trackPlatformHealth,
  trackMilestone,
  trackChurnSignal,
  trackPlatformCombination,
  trackUsageTime,
  trackSyncFlow,
  trackFeatureDiscovery,
  trackImplicitFeedback,
  trackHelpSeeking,
  trackGrowthMetrics,
  updateCumulativeStats,
  trackSessionDepth,
  trackPlatformExpansion,
}
