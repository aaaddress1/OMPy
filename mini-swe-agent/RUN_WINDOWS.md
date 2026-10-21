# Oh My PiPy (OMPy) Windows Console Agent

從上一層 OMPy 專案目錄執行 `install.ps1` 或 `start.cmd`。Python 與依賴安裝在上一層的 `.tools/agent-env/`，以 `PYTHONPATH` 載入這份原始碼。

## 按 F5 啟動

在 VS Code 開啟 OMPy 或其中的 `mini-swe-agent` 資料夾，按 **F5**。
啟動項目是 **Oh My PiPy (OMPy)：Console Agent**，畫面出現在整合式終端機。
看到 `你>` 後直接輸入任務並按 Enter。

```text
你> 用 PowerShell 列出目前目錄，並用中文解釋結果
你> /reasoning low
你> 請依照剛剛的結果繼續分析
你> /new
你> /exit
```

模型名稱、API 網址與 key 由使用者設定，使用支援工具呼叫的 Chat Completions API。完整安裝與 API 設定命令見上一層 `README.md`。
預設 `reasoning_effort=none`；可切換 `low` 或 `high`。
上下文容量 1048576，單次輸出上限 8192。設定在本機 `.env`，API key 不納入版本控制。

工具會實際執行 Windows PowerShell 命令，也能透過 `& '工具完整路徑.exe' ...` 呼叫程式。
每次工具呼叫都是新的 PowerShell 程序，當前目錄及程序內變數不跨呼叫保存。

## 終端機啟動

在 `mini-swe-agent` 目錄執行：

```powershell
. ..\scripts\project-env.ps1 -ProjectRoot (Resolve-Path ..).Path
& $env:HARNESS_PYTHON -X utf8 -m minisweagent.run.windows_console
```

單次任務：

```powershell
& $env:HARNESS_PYTHON -X utf8 -m minisweagent.run.windows_console --reasoning low --task "用 PowerShell 確認目前目錄"
```

## 要改上下文時

- `src/minisweagent/context_manager.py` 的 `ContextManager.prepare()`：每次模型請求前選取及重組 messages。
- `src/minisweagent/models/windows_host.py`：接收 C 的結果，組成 Chat Completions 請求。
- `src/minisweagent/environments/powershell.py`：執行命令、保存完整文字輸出、回傳片段及來源位置。
- 原本的 `src/minisweagent/agents/default.py`：仍負責模型 → 工具 → 模型的主迴圈。

每次啟動建立 `.runs/<session>/`，每個任務底下保存：

- `request-*.json`：實際送出的上下文與模型參數，不含 API key。
- `tool-*.json`：命令、完整解碼後文字輸出、退出碼。
- `trajectory.traj.json`：該任務的完整執行軌跡與原始模型回應。

預設 compact 策略以估算 token 預算及伺服器用量校正，在背景整理舊紀錄；接近上限時等待摘要，保留近期工具組與當前任務原文。原始紀錄保存在磁碟上。sliding 實驗策略則移除舊任務與工具組。
這還不是 Qwen 原生 tokenizer 的精確計數，也尚未實作語意檢索與重啟後對話還原。
工具文字超過 6000 bytes 時只回傳前段，模型可按 archive 路徑再讀取完整文字。
這些文字紀錄不能取代鑑識證據原始檔案與原始位元組保存。
