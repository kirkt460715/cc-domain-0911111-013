# Trialvault 试验数据版本库

使用内容寻址文件和本地目录库保存试验数据。支持结构契约、导入、投影连接、汇总、质量检查和经过批准的版本导出；撤回状态会影响发布资格。

## 运行环境

使用 Python 3.11，核心功能依赖标准库和 SQLite。演示和测试只使用本地合成数据，不需要账户、设备或外部运行服务。请从项目根目录执行命令。

## 检查与运行

构建检查验证语法，测试使用临时数据目录，不会连接远端服务。

```sh
python3 -m compileall -q trialvault
python3 -m unittest discover -s tests -v
python3 -m trialvault demo
```

演示会展示产品的实际数据处理和输出，运行结束自行退出。查看现有接口：

```sh
python3 -m trialvault --help
```

## 目录与数据

trialvault/ 保存领域逻辑和请求入口，tests/ 包含正常行为、边界与恢复测试。例子中的标识和记录为合成资料，不包含真实人员或生产测量。业务目录和本地数据库由调用者指定，应与源码分开保存。

## 分析计划锁定

面向先锁方案再看结果的试验复盘。分析计划指向固定版本的随机分组数据，写明实验单位、分层、主次指标、观察截止和允许的预处理（重复测量取舍规则、预先声明的取值界限）。计划本身不可变：任何修改必须另建版本，新版本记录父版本指纹和修改原因；结果数据可以晚到，计划定义时不需要结果存在。

分析按原分组执行（意向处理），交叉处理、未测和重复测试分别报告。主指标支持连续均值差和二元事件率差，按原始入组分层权重汇总，显著性用保持分层和单位的随机化置换检验（固定种子、可复现）；区间假设随结果说明，次指标在同一族内做 Holm 校正。协变量调整只使用入组前数据，并记录拟合样本。空层、恒定值、不平衡和截止后结果都在结果中逐条解释。

导出的分析包包含锁定计划、输入版本、样本去留和随机种子，可离线校验；晚到的数据只能形成新的数据版本和新的运行，不会原地改写已冻结的计划或结果。

```sh
# 动作：analysis_plan_define / analysis_plan_revise / analysis_plan /
# analysis_plan_versions / analysis_run / analysis_run_get /
# analysis_package / analysis_package_verify
python3 -m trialvault --root vault-data run plan-define.json
```

## 干净环境复现

容器定义固定运行时版本。构建时准备环境，运行时无需网络；每次启动会复制只读源码到临时工作目录，测试产生的文件不会写回仓库。

```sh
docker build -t local-product-check .
docker run --rm --network none local-product-check
```
