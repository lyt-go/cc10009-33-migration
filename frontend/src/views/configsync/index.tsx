import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/configsync'
const columns = [
  "任务编号",
  "源节点",
  "目标节点",
  "同步类型",
  "最后同步时间",
  "配置版本号",
  "差异摘要",
  "同步状态"
]
const actions = [
  "发起同步",
  "确认完成",
  "标记失败"
]
const statuses = [
  "草稿",
  "同步中",
  "已完成",
  "已失败"
]

type Row = Record<string, string | number | boolean | null | undefined>
type Stage = Record<string, string | number | boolean | null | undefined>
type Evidence = Record<string, string | number | boolean | null | undefined>
type Conflict = Record<string, string | number | boolean | null | undefined>

type StageResult = {
  任务ID: number
  任务编号: string
  源节点: string
  目标节点: string
  配置版本号: string
  载荷版本: string
  阶段总数: number
  已应用阶段数: number
  已确认阶段数: number
  未应用阶段数: number
  待确认阶段数: number
  待处理冲突数: number
  结果摘要: string
  两组版本依据: boolean
  版本依据说明: string | null
  版本依据: Evidence[]
  冲突记录: Conflict[]
  阶段: Stage[]
  可暴露节点版本: Record<string, unknown> | null
  本次动作?: { 动作: string; 阶段序号: number; 重复提交: boolean; 说明: string }
}

type Manifest = {
  节点编号: string
  已完整应用版本: Record<string, unknown> | null
  可下发: boolean
  清单摘要: string
  待处理冲突数: number
  结果摘要: { 任务编号: string; 阶段总数: number; 已应用阶段数: number }[]
}

function stageTone(stage: Stage): string {
  if (stage["阶段状态"] === "待处理冲突") return 'chip warn'
  if (stage["阶段状态"] === "已完成") return 'chip ok'
  if (stage["应用状态"] === "已应用") return 'chip pending'
  return 'chip'
}

export default function ConfigsyncView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selected, setSelected] = useState<Row | null>(null)
  const [dataset, setDataset] = useState<StageResult | null>(null)
  const [manifest, setManifest] = useState<Manifest | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('同步任务列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '配置同步列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function loadResult(row: Row) {
    setSelected(row)
    setDataset(null)
    setManifest(null)
    setNotice('')
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/result`)
      if (response.ok) {
        setDataset(await response.json())
      } else {
        const detail = await response.json().catch(() => ({}))
        setNotice(detail.detail ?? '该任务还没有分阶段同步结果，可先执行迁移')
      }
      const terminal = await request(`${ENDPOINT}/${row.id}/terminal`)
      if (terminal.ok) {
        setManifest(await terminal.json())
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分阶段同步结果读取失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('配置同步动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '配置同步操作失败')
    }
  }

  async function migrate(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/migrate`, {
        method: 'POST',
        body: JSON.stringify({ values: {} }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '迁移未生成分阶段同步结果')
      }
      setNotice(payload.message ?? '分阶段同步结果已生成')
      await reload()
      await loadResult(row)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '迁移失败')
    }
  }

  async function stageAction(row: Row, stage: Stage, action: '应用阶段' | '确认阶段') {
    setErrorMessage('')
    setNotice('')
    const key = action === '应用阶段' ? stage["应用幂等标识"] : stage["确认幂等标识"]
    try {
      const response = await request(`${ENDPOINT}/${row.id}/stages/${stage["阶段序号"]}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action, 幂等标识: key } }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '阶段操作未生效')
      }
      setNotice(payload.message ?? '阶段操作已完成')
      if (payload.entry) {
        setDataset(payload.entry)
      }
      await reload()
      const terminal = await request(`${ENDPOINT}/${row.id}/terminal`)
      if (terminal.ok) {
        setManifest(await terminal.json())
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '阶段操作失败')
    }
  }

  async function replay(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/replay`, {
        method: 'POST',
        body: JSON.stringify({ values: {} }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '重放未完成')
      }
      setNotice(payload.message ?? '旧任务已重放')
      if (payload.entry) {
        setDataset(payload.entry)
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '旧任务重放失败')
    }
  }

  const stats = [
    { label: "同步任务总数", value: total },
    { label: "已完成任务", value: rows.filter((row) => row.status === '已完成').length },
    { label: "失败任务数", value: rows.filter((row) => row.status === '已失败').length },
    { label: "待处理冲突", value: rows.reduce((sum, row) => sum + Number(row["待处理冲突数"] ?? 0), 0) },
  ]

  return (
    <section className="page" data-module="configsync">
      <header className="page-head">
        <div>
          <h2>配置同步管理</h2>
          <p className="page-desc">
            旧载荷按段迁移为分阶段同步结果：中断后只续做未应用段，同一阶段重复提交按幂等处理，
            工作终端只暴露一个已完整应用的节点配置版本。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('同步任务登记入口尚未接入审批流')}>
            登记同步任务
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出配置同步清单
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/results`, '_blank')}>
            导出分阶段结果数据集
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>载荷/阶段</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={selected?.id === row.id ? 'row-active' : undefined}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>
                <span className="chip">{String(row["载荷版本"] ?? 'legacy-v1')}</span>
                {row["分阶段结果已生成"] ? (
                  <span className="chip ok">
                    {String(row["已应用阶段数"] ?? 0)}/{String(row["阶段总数"] ?? 0)} 段
                  </span>
                ) : (
                  <span className="chip">未迁移</span>
                )}
                {Number(row["待处理冲突数"] ?? 0) > 0 && (
                  <span className="chip warn">冲突 {String(row["待处理冲突数"])}</span>
                )}
              </td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void loadResult(row)}>分阶段结果</button>
                <button className="link" type="button" onClick={() => void migrate(row)}>迁移</button>
                <button className="link" type="button" onClick={() => void replay(row)}>重放</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无配置同步数据，可先登记同步任务</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条配置同步记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
        {!errorMessage && notice && <span>{notice}</span>}
      </footer>

      {selected && (
        <section className="detail-panel">
          <header className="detail-head">
            <h3>分阶段同步结果 · {String(selected["任务编号"])}</h3>
            <button className="btn ghost" type="button" onClick={() => { setSelected(null); setDataset(null); setManifest(null) }}>
              收起
            </button>
          </header>

          {!dataset && <p className="page-desc">{notice || '暂无分阶段同步结果，可先对该任务执行迁移。'}</p>}

          {dataset && (
            <>
              <div className="stat-row">
                <article className="stat-card">
                  <span className="stat-label">阶段进度</span>
                  <strong className="stat-value">{dataset.已应用阶段数}/{dataset.阶段总数}</strong>
                </article>
                <article className="stat-card">
                  <span className="stat-label">已确认</span>
                  <strong className="stat-value">{dataset.已确认阶段数}</strong>
                </article>
                <article className="stat-card">
                  <span className="stat-label">待处理冲突</span>
                  <strong className="stat-value">{dataset.待处理冲突数}</strong>
                </article>
                <article className="stat-card">
                  <span className="stat-label">结果摘要</span>
                  <strong className="stat-value detail-summary">{dataset.结果摘要}</strong>
                </article>
              </div>

              <table className="data-table">
                <thead>
                  <tr>
                    <th>阶段</th>
                    <th>阶段名</th>
                    <th>应用状态</th>
                    <th>确认状态</th>
                    <th>来源</th>
                    <th>应用幂等标识</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {dataset.阶段.map((stage) => (
                    <tr key={String(stage.id)}>
                      <td>{String(stage["阶段序号"])}</td>
                      <td>{String(stage["阶段名"])}</td>
                      <td><span className={stageTone(stage)}>{String(stage["应用状态"])}</span></td>
                      <td>{String(stage["确认状态"])}</td>
                      <td>{String(stage["来源"] ?? '—')}</td>
                      <td className="mono">{String(stage["应用幂等标识"])}</td>
                      <td className="row-actions">
                        <button
                          className="link"
                          type="button"
                          disabled={stage["应用状态"] === '已应用' || stage["阶段状态"] === '待处理冲突'}
                          onClick={() => void stageAction(selected, stage, '应用阶段')}
                        >
                          应用阶段
                        </button>
                        <button
                          className="link"
                          type="button"
                          disabled={stage["应用状态"] !== '已应用' || stage["确认状态"] === '已确认'}
                          onClick={() => void stageAction(selected, stage, '确认阶段')}
                        >
                          确认阶段
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <div className="detail-grid">
                <div>
                  <h4>版本依据（{dataset.版本依据.length} 组）</h4>
                  {dataset.版本依据.length === 0 && <p className="page-desc">暂无可读版本依据。</p>}
                  {dataset.版本依据.length > 0 && (
                    <table className="data-table">
                      <thead>
                        <tr><th>依据类型</th><th>来源</th><th>版本</th><th>完整</th><th>节点已落库</th><th>时间</th></tr>
                      </thead>
                      <tbody>
                        {dataset.版本依据.map((item, index) => (
                          <tr key={`${item["依据类型"]}-${index}`}>
                            <td>{String(item["依据类型"])}</td>
                            <td>{String(item["来源"])}</td>
                            <td>{String(item["配置版本号"])}</td>
                            <td>{item["完整"] ? '是' : '否'}</td>
                            <td>{item["节点已落库"] ? '是' : '否'}</td>
                            <td>{String(item["时间"] ?? '—')}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                  {dataset.两组版本依据 && dataset.版本依据说明 && (
                    <p className="warn-text">{dataset.版本依据说明}</p>
                  )}
                </div>
                <div>
                  <h4>待处理记录（{dataset.冲突记录.length}）</h4>
                  {dataset.冲突记录.length === 0 && <p className="page-desc">没有待处理记录。</p>}
                  {dataset.冲突记录.length > 0 && (
                    <table className="data-table">
                      <thead>
                        <tr><th>类型</th><th>状态</th><th>原因</th></tr>
                      </thead>
                      <tbody>
                        {dataset.冲突记录.map((item) => (
                          <tr key={String(item.id)}>
                            <td>{String(item["冲突类型"])}</td>
                            <td><span className={item["状态"] === '待处理' ? 'chip warn' : 'chip ok'}>{String(item["状态"])}</span></td>
                            <td>{String(item["原因"])}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              </div>

              {manifest && (
                <div className="terminal-card">
                  <h4>工作终端侧清单 · {manifest.节点编号}</h4>
                  <p className="page-desc">
                    已完整应用版本：
                    {manifest.已完整应用版本 ? (
                      <strong>{String(manifest.已完整应用版本["配置版本号"])}</strong>
                    ) : (
                      <strong>无（尚未完整应用）</strong>
                    )}
                    {' '}· 可下发：{manifest.可下发 ? '是' : '否'} · 清单摘要：
                    <span className="mono">{manifest.清单摘要}</span>
                  </p>
                  {manifest.结果摘要.length > 0 && (
                    <p className="page-desc">
                      结果摘要：
                      {manifest.结果摘要
                        .map((item) => `${item.任务编号} ${item.已应用阶段数}/${item.阶段总数} 段`)
                        .join('；')}
                    </p>
                  )}
                </div>
              )}
            </>
          )}
        </section>
      )}
    </section>
  )
}
