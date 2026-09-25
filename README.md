# 规程真值维护台(TMS)

一个基于**理由真值维护(JTMS)**的规程管理服务:安全员在页面上编辑由
**唯一标识事实**与**无变量规则**组成的规程,规则前提可标为**肯定或否定**
(否定前提 = "仅当某原始事实或结论不成立时才放行"的例外条件),系统实时
维护每个结论的有效性、完整依据,并在事实被撤回时于**同一持久化事务**内
沿反向索引传播失效。

纯 Python 标准库实现,镜像构建零外部依赖。

## 语义

- 结论有效 ⟺ 它处于当前有效事实上的规则**最小不动点**中;否定前提按
  **分层(stratified)**方式求值:逐依赖层计算最小不动点,否定前提总在
  其节点定值之后读取。
- 肯定前提要求节点成立;否定前提(`{"id":"B","polarity":"neg"}`,页面录入
  用 `!B`)要求节点**确认缺席**(已撤回或未导出)。规则触发 ⟺ 全部肯定
  前提成立且全部否定前提缺席。
- 每次规则触发都把**带极性的完整前提集合**存入 `supports` 表;结论依据中
  可见实际满足的肯定项与被确认缺席的否定项。
- 断言原先缺席的阻断事实 → 依赖其缺席的结论及下游在**同一事务**内失效,
  传播链以 `negative-premise-blocked` 标注并给出 `blocked_by`;撤回阻断
  事实 → 其他前提仍满足的结论恢复,负向支持与原有正向支持并存。
- 撤回事实 → 经 `rule_premises` 反向索引找到受影响结论子图 → 在子图上
  分层重算最小不动点 → 同一事务提交节点状态、支持状态、裁决与传播链。
- 结论只有在**没有任何完整支持**时才失效;循环规则没有落地事实支撑时
  不会凭空有效,落地支撑被撤回后随之坍塌。
- 重复撤回已失效事实:稳定回放首次撤回时记录的裁决(`replayed: true`)。
- 未知事实、引用不存在节点的规则、自我支持闭环(`A ⇒ A`)、**含否定边的
  依赖环**(如 `¬Y ⇒ X, X ⇒ Y`)均被拒绝,且事务回滚,规程不被污染。
  纯正向多节点循环仍可声明,整批原子生效。
- 全部状态存于 SQLite,重启后结论与依据状态原样保留。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/healthz` | 健康检查,反映接口/存储可用性(200/503) |
| GET | `/api/state` | 事实、规则(含被阻断的否定前提)、结论及其支持、已撤回事实、最近事件 |
| POST | `/api/facts` | 添加事实 `{id, label?}` |
| POST | `/api/rules` | 添加单条规则或 JSON 数组批量原子添加;前提为节点 id 或 `{"id","polarity":"neg"}` |
| POST | `/api/facts/{id}/retract` | 撤回事实,返回裁决(失效结论、剩余依据、传播链、恢复结论) |
| POST | `/api/facts/{id}/assert` | 恢复事实;若系阻断事实,返回依赖其缺席而失效的结论与传播链 |
| GET | `/api/conclusions/{id}/justification` | 结论的完整递归依据树(否定前提标注极性与缺席确认) |
| POST | `/api/reset` | 清空规程(仅 `TMS_ALLOW_RESET=1` 时可用) |

## 本地运行

```bash
python3 web/build.py --out web/dist            # 构建页面
TMS_ALLOW_RESET=1 PAGE_DIR=web/dist PORT=8000 python3 -m app.server
# 打开 http://localhost:8000
```

运行规则逻辑测试与端到端验证(测试 + 构建页面 + HTTP 冒烟):

```bash
APP_BASE_URL=http://localhost:8000 PAGE_OUT=web/dist python3 -m verify.run
echo $?   # 0 = 全部通过
```

## Docker Compose

```bash
# 启动应用(页面经可配置宿主机端口访问,默认 8080)
TMS_HOST_PORT=8080 docker compose up app

# 一键验证:规则逻辑测试 → 构建页面 → API/HTTP 冒烟,退出码即结果
docker compose up --exit-code-from verify --abort-on-container-exit
```

- `app`:API + 页面。数据存于命名卷 `tms_data`(重启保留);
  页面由 `page_dist` 卷提供(verify 构建),卷为空时回退到镜像内置页面。
- `verify`:依赖 `app` 健康后单次执行验证流水线并以退出码报告结果,
  随后退出。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `8000` | 容器内监听端口 |
| `TMS_HOST_PORT` | `8080` | 宿主机映射端口(compose) |
| `TMS_DB_PATH` | `/data/tms.db` | SQLite 数据库路径 |
| `PAGE_DIR` | `/srv/page` | 页面构建产物目录 |
| `TMS_ALLOW_RESET` | `0` | 是否开放 `/api/reset` |

## 目录

```
app/tms/engine.py   JTMS 引擎(反向索引传播、最小不动点、传播链、裁决)
app/tms/store.py    SQLite 持久化(单事务边界)
app/server.py       标准库 HTTP 服务(API + 静态页面)
app/tests/          规则逻辑单元测试
web/src/            页面源码;web/build.py 构建(打构建戳)
verify/run.py       一键验证流水线(测试 + 构建 + 冒烟,退出码报告)
```
