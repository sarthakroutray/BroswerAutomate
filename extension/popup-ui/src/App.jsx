import React, { useState, useEffect, useRef } from 'react';
import { Settings, Clock, Zap, Play, Square, Settings2, Trash2, Command, Search, Navigation } from 'lucide-react';

const isDev = typeof chrome === 'undefined' || !chrome.runtime;

function App() {
  const [activeTab, setActiveTab] = useState('main');
  const [connected, setConnected] = useState(false);
  const [tabCount, setTabCount] = useState(0);
  const [serverUrl, setServerUrl] = useState('ws://localhost:8000');
  const [running, setRunning] = useState(false);
  const [goal, setGoal] = useState('');
  const [maxSteps, setMaxSteps] = useState(30);
  const [history, setHistory] = useState([]);
  const [errorMsg, setErrorMsg] = useState('');

  // Progress State
  const [progressStep, setProgressStep] = useState(0);
  const [progressMax, setProgressMax] = useState(30);
  const [logs, setLogs] = useState([]);
  const [result, setResult] = useState(null);

  const logContainerRef = useRef(null);

  useEffect(() => {
    if (isDev) return;

    chrome.storage.local.get(['maxSteps', 'taskHistory'], (res) => {
      if (res.maxSteps) setMaxSteps(res.maxSteps);
      if (res.taskHistory) setHistory(res.taskHistory);
    });

    chrome.runtime.sendMessage({ type: 'GET_STATUS' }, (res) => {
      if (chrome.runtime.lastError) return;
      if (res) {
        setConnected(res.connected);
        setTabCount(res.tabCount || 0);
        if (res.serverUrl) setServerUrl(res.serverUrl);
        if (res.running) setRunning(true);
      }
    });

    const messageListener = (msg) => {
      switch (msg.type) {
        case 'CONNECTION_STATUS':
          setConnected(msg.connected);
          setTabCount(msg.tabCount || 0);
          if (msg.serverUrl) setServerUrl(msg.serverUrl);
          break;
        case 'TASK_PROGRESS':
          if (msg.step !== undefined) {
            setProgressStep(msg.step);
            setProgressMax(msg.maxSteps || msg.max_steps || maxSteps);
          }
          if (msg.description || msg.message) {
            addLog(msg.description || msg.message);
          }
          if (['completed', 'error', 'stopped', 'max_steps'].includes(msg.status)) {
            finishProgress(msg.status, msg.result || msg.summary || msg.message);
          }
          break;
        case 'TASK_LOG':
          addLog(msg.text);
          break;
        case 'TASK_ERROR':
          showError(msg.error);
          setRunning(false);
          break;
      }
    };

    chrome.runtime.onMessage.addListener(messageListener);
    return () => chrome.runtime.onMessage.removeListener(messageListener);
  }, []);

  useEffect(() => {
    if (logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
    }
  }, [logs]);

  const addLog = (text) => {
    const time = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    setLogs(prev => [...prev.slice(-49), { time, text }]);
  };

  const showError = (text) => {
    setErrorMsg(text);
    setTimeout(() => setErrorMsg(''), 8000);
  };

  const finishProgress = (statusCode, msg) => {
    setRunning(false);
    let type = 'error';
    let text = 'Task failed';

    if (statusCode === 'completed') {
      type = 'success';
      text = '✓ ' + (msg || 'Task completed successfully');
    } else if (statusCode === 'error') {
      text = '✕ ' + (msg || 'Task failed');
    } else if (statusCode === 'max_steps') {
      text = '⚠ ' + (msg || 'Task stopped at maximum limit');
    } else if (statusCode === 'stopped') {
      text = '⬛ Task interrupted';
    }

    setResult({ type, text });

    if (!isDev) {
      chrome.storage.local.get(['taskHistory'], (res) => {
        const hist = res.taskHistory || [];
        hist.unshift({ goal, status: statusCode, time: Date.now() });
        if (hist.length > 30) hist.length = 30;
        chrome.storage.local.set({ taskHistory: hist });
        setHistory(hist);
      });
    }
  };

  const handleConnect = async () => {
    if (isDev) { setConnected(!connected); return; }
    if (connected) {
      chrome.runtime.sendMessage({ type: 'DISCONNECT' });
    } else {
      try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab) {
          chrome.runtime.sendMessage({ type: 'CONNECT', tabId: tab.id });
        } else {
          showError('No active tab found');
        }
      } catch (err) {
        showError('Failed to get active tab: ' + err.message);
      }
    }
  };

  const handleRunTask = (quickGoal = null) => {
    const taskGoal = quickGoal || goal.trim();
    if (!taskGoal) {
      showError('Please enter a task or select a quick action.');
      return;
    }

    setErrorMsg('');
    setRunning(true);
    if (!quickGoal) setGoal(taskGoal);

    setLogs([]);
    setResult(null);
    setProgressStep(0);

    if (isDev) return;

    chrome.runtime.sendMessage({
      type: 'RUN_TASK',
      goal: taskGoal,
      maxSteps
    }, (res) => {
      if (chrome.runtime.lastError) {
        showError('Failed to send task: ' + chrome.runtime.lastError.message);
        setRunning(false);
        return;
      }
      if (res && res.error) {
        showError(res.error);
        setRunning(false);
      }
    });
  };

  const handleStopTask = () => {
    if (isDev) { setRunning(false); return; }
    chrome.runtime.sendMessage({ type: 'STOP_TASK' });
  };

  const handleClearHistory = () => {
    if (!isDev) chrome.storage.local.set({ taskHistory: [] });
    setHistory([]);
  };

  const saveServerUrl = (e) => {
    const candidate = e.target.value.trim();
    if (!candidate) return;
    setServerUrl(candidate);
    if (!isDev) chrome.runtime.sendMessage({ type: 'SET_SERVER_URL', serverUrl: candidate });
  };

  const saveMaxSteps = (e) => {
    const val = parseInt(e.target.value);
    setMaxSteps(val);
    if (!isDev) chrome.storage.local.set({ maxSteps: val });
  };

  const timeAgo = (ts) => {
    const s = Math.floor((Date.now() - ts) / 1000);
    if (s < 60) return 'just now';
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h`;
    return `${Math.floor(h / 24)}d`;
  };

  return (
    <div className="h-[600px] w-full relative flex flex-col font-[Inter,-apple-system,sans-serif]">
      {/* Background Orbs */}
      <div className="mesh-blob blob-1"></div>
      <div className="mesh-blob blob-2"></div>
      <div className="mesh-blob blob-3"></div>

      {/* Main Glass Layer overlaying everything */}
      <div className="absolute inset-x-2 inset-y-2 rounded-[2rem] border border-glass-border bg-glass-base/50 shadow-2xl overflow-hidden flex flex-col backdrop-blur-[40px] z-10">

        {/* HEADER */}
        <div className="flex-none px-6 pt-6 pb-4">
          <div className="flex items-center justify-between mb-5">
            <div className="flex items-center gap-3 font-semibold text-lg tracking-tight text-text-main drop-shadow-sm">
              <div className="w-8 h-8 rounded-full bg-white/20 backdrop-blur-md flex items-center justify-center border border-white/30 shadow-[0_4px_12px_rgba(0,0,0,0.2)]">
                <Zap size={16} className="text-white drop-shadow-md" fill="white" />
              </div>
              AntiGravity
            </div>

            <button
              onClick={handleConnect}
              className={`flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-semibold backdrop-blur-md border transition-all duration-300 ${connected
                  ? 'bg-success/20 border-success/30 text-success hover:bg-success/30'
                  : 'bg-white/10 border-white/20 text-white hover:bg-white/20 hover:scale-105'
                }`}
            >
              <div className={`w-1.5 h-1.5 rounded-full ${connected ? 'bg-success shadow-[0_0_6px_#10b981]' : 'bg-danger shadow-[0_0_6px_#ef4444]'}`}></div>
              {connected ? 'Active' : 'Disconnected'}
            </button>
          </div>

          {/* MacOS/iOS style segmented control */}
          <div className="flex p-1 bg-black/30 backdrop-blur-md rounded-xl border border-white/10 shadow-inner">
            <button
              onClick={() => setActiveTab('main')}
              className={`flex-1 py-1.5 rounded-lg text-xs font-medium transition-all duration-300 ${activeTab === 'main' ? 'bg-white/20 text-white shadow-md' : 'text-white/60 hover:text-white hover:bg-white/10'}`}
            >
              Console
            </button>
            <button
              onClick={() => setActiveTab('history')}
              className={`flex-1 py-1.5 rounded-lg text-xs font-medium transition-all duration-300 ${activeTab === 'history' ? 'bg-white/20 text-white shadow-md' : 'text-white/60 hover:text-white hover:bg-white/10'}`}
            >
              History
            </button>
            <button
              onClick={() => setActiveTab('settings')}
              className={`flex-1 py-1.5 rounded-lg text-xs font-medium transition-all duration-300 ${activeTab === 'settings' ? 'bg-white/20 text-white shadow-md' : 'text-white/60 hover:text-white hover:bg-white/10'}`}
            >
              Systems
            </button>
          </div>
        </div>

        {/* scrollable body */}
        <div className="flex-1 overflow-y-auto px-6 pb-6 pt-2 custom-scroll">

          {/* === MAIN TAB === */}
          {activeTab === 'main' && (
            <div className="animate-in fade-in slide-in-from-bottom-3 duration-500">

              {/* Errors */}
              {errorMsg && (
                <div className="bg-danger/20 backdrop-blur-md border border-danger/30 rounded-2xl p-4 mb-5 text-xs text-red-200 leading-relaxed shadow-lg">
                  {errorMsg}
                </div>
              )}

              {/* Quick Actions (Glass Buttons) */}
              <div className="grid grid-cols-2 gap-3 mb-6">
                {[
                  { label: 'Summarize text', icon: <Search size={16} />, action: 'Extract and summarize all visible text content from this page' },
                  { label: 'Fill out inputs', icon: <Settings2 size={16} />, action: 'Fill out the form on this page with reasonable test data' }
                ].map((btn, i) => (
                  <button
                    key={i}
                    disabled={!connected || running}
                    onClick={() => handleRunTask(btn.action)}
                    className="flex items-center gap-2.5 p-3.5 bg-white/5 border border-white/10 backdrop-blur-xl rounded-[1rem] text-left transition-all duration-300 hover:scale-[1.03] hover:bg-white/10 hover:border-white/20 hover:shadow-xl disabled:opacity-40 disabled:hover:scale-100 group"
                  >
                    <div className="p-1.5 bg-white/10 rounded-lg text-white/80 group-hover:text-white transition-colors">{btn.icon}</div>
                    <span className="text-xs font-medium text-white/90">{btn.label}</span>
                  </button>
                ))}
              </div>

              {/* Big Glass Prompt Input */}
              <div className="relative mb-5 group">
                <textarea
                  value={goal}
                  onChange={(e) => setGoal(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                      e.preventDefault();
                      if (connected && !running) handleRunTask();
                    }
                  }}
                  placeholder="Ask the AI to perform a task on the current page..."
                  disabled={running}
                  className="w-full min-h-[110px] p-4 pt-5 pb-8 bg-black/30 backdrop-blur-xl border border-white/10 rounded-[1.25rem] text-[13px] text-white/90 leading-relaxed resize-y focus:outline-none focus:border-white/30 focus:bg-black/40 disabled:opacity-50 transition-all placeholder-white/30 shadow-inner"
                />

                <div className="absolute top-4 right-4 text-white/20">
                  <Navigation size={18} />
                </div>

                <div className="absolute bottom-3 left-4 flex gap-1.5 items-center pointer-events-none opacity-50">
                  <Command size={11} className="text-white" /> <span className="text-[10px] font-medium text-white">Return to send</span>
                </div>
              </div>

              {/* Primary Run Button */}
              <div className="flex gap-3 mb-6">
                <button
                  disabled={!connected || running}
                  onClick={() => handleRunTask()}
                  className="flex-[3] py-3.5 bg-white/20 backdrop-blur-lg border border-white/20 text-white rounded-[1rem] font-semibold text-[13px] transition-all duration-300 hover:scale-[1.02] hover:bg-white/30 hover:border-white/40 hover:shadow-[0_0_20px_rgba(255,255,255,0.2)] disabled:opacity-40 disabled:hover:scale-100 flex items-center justify-center gap-2"
                >
                  <Play size={16} className="fill-white" /> Execute Instructions
                </button>
                <button
                  disabled={!running}
                  onClick={handleStopTask}
                  className="flex-1 py-3.5 bg-danger/70 backdrop-blur-lg border border-danger/40 text-white rounded-[1rem] font-semibold text-[13px] transition-all duration-300 hover:scale-[1.02] hover:bg-danger disabled:opacity-20 flex items-center justify-center shadow-[0_0_15px_rgba(239,68,68,0.2)]"
                >
                  <Square size={16} className="fill-white" />
                </button>
              </div>

              {/* Fluid Progress/Activity Display */}
              {(running || logs.length > 0 || result) && (
                <div className="bg-black/40 backdrop-blur-2xl border border-white/10 rounded-[1.25rem] overflow-hidden animate-in slide-in-from-bottom-4 duration-500 shadow-2xl">
                  <div className="px-5 py-3.5 flex justify-between items-center bg-white/5 border-b border-white/10">
                    <div className="text-xs font-medium text-white/80 flex items-center gap-2">
                      {running ? (
                        <div className="relative flex h-2.5 w-2.5">
                          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-accent opacity-75"></span>
                          <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-accent shadow-[0_0_10px_#3b82f6]"></span>
                        </div>
                      ) : (
                        <div className="w-2 h-2 rounded-full bg-white/40"></div>
                      )}
                      {running ? 'Agent Analyzing Page' : 'Execution Finished'}
                    </div>
                    <div className="text-[10px] bg-white/10 px-2 py-0.5 rounded-full font-mono text-white/70 tracking-wider">
                      {progressStep}/{progressMax}
                    </div>
                  </div>

                  {/* Neon sleek progress bar */}
                  <div className="h-[2px] bg-black/50 overflow-hidden relative">
                    <div
                      className="absolute inset-x-0 bottom-0 h-full bg-gradient-to-r from-accent to-purple-500 transition-all duration-500 ease-out shadow-[0_0_8px_#3b82f6]"
                      style={{ width: `${Math.min((progressStep / progressMax) * 100, 100)}%` }}
                    />
                  </div>

                  <div ref={logContainerRef} className="h-[120px] overflow-y-auto p-4 space-y-1.5 font-mono text-[11px] selection:bg-white/20 tracking-tight custom-scroll">
                    {logs.map((log, i) => (
                      <div key={i} className={`flex items-start gap-2.5 transition-colors ${i === logs.length - 1 ? 'text-white' : 'text-white/50'}`}>
                        <span className="text-accent/60 shrink-0 select-none mt-0.5">{log.time.substring(0, 5)}</span>
                        <span className="leading-snug">{log.text}</span>
                      </div>
                    ))}
                  </div>

                  {result && (
                    <div className={`px-5 py-3 text-xs font-semibold backdrop-blur-md border-t border-white/10 ${result.type === 'success' ? 'bg-success/10 text-emerald-300' : 'bg-danger/10 text-red-300'} flex items-center justify-center`}>
                      {result.text}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* === HISTORY TAB === */}
          {activeTab === 'history' && (
            <div className="animate-in fade-in slide-in-from-bottom-3 duration-500 space-y-3">
              {history.length === 0 ? (
                <div className="py-20 text-center text-white/40 text-[13px] font-medium flex flex-col items-center gap-3">
                  <div className="w-16 h-16 rounded-full bg-white/5 flex items-center justify-center mb-2 shadow-inner border border-white/5">
                    <Clock size={28} className="opacity-50" />
                  </div>
                  No prior executions
                </div>
              ) : (
                <>
                  {history.map((item, i) => (
                    <div
                      key={i}
                      onClick={() => { setGoal(item.goal); setActiveTab('main'); }}
                      className="bg-black/30 backdrop-blur-md border border-white/10 rounded-2xl p-4 cursor-pointer transition-all duration-300 hover:bg-white/10 hover:border-white/20 hover:scale-[1.02] shadow-sm group"
                    >
                      <div className="text-[12px] font-medium text-white/80 leading-relaxed max-h-[36px] overflow-hidden group-hover:text-white transition-colors">
                        „{item.goal}‟
                      </div>
                      <div className="flex justify-between items-center mt-3 pt-3 border-t border-white/5">
                        <span className={`text-[10px] tracking-wider uppercase font-bold ${item.status === 'completed' ? 'text-emerald-400' : item.status === 'error' ? 'text-red-400' : 'text-amber-400'}`}>
                          {item.status}
                        </span>
                        <span className="text-white/30 text-[10px] font-medium flex items-center gap-1.5">
                          {timeAgo(item.time)}
                        </span>
                      </div>
                    </div>
                  ))}
                  <button
                    onClick={handleClearHistory}
                    className="w-full mt-2 py-3.5 flex items-center justify-center gap-2 text-xs font-semibold text-danger/70 border border-danger/20 rounded-2xl hover:bg-danger/10 transition-colors group"
                  >
                    <Trash2 size={14} className="group-hover:stroke-danger transition-colors" /> Wipe memory
                  </button>
                </>
              )}
            </div>
          )}

          {/* === SETTINGS TAB === */}
          {activeTab === 'settings' && (
            <div className="space-y-6 animate-in fade-in slide-in-from-bottom-3 duration-500">

              <div className="bg-black/30 backdrop-blur-xl border border-white/10 rounded-[1.25rem] p-5 shadow-inner">
                <label className="flex items-center gap-2 text-[12px] font-semibold text-white mb-4">
                  <div className="p-1 rounded bg-accent/20 text-accent"><Settings2 size={14} /></div>
                  Action Horizon
                </label>
                <div className="flex items-center gap-4">
                  <input
                    type="range" min="1" max="60" value={maxSteps} onChange={saveMaxSteps}
                    className="flex-1 h-1 bg-white/10 rounded-full appearance-none flex cursor-pointer [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-5 [&::-webkit-slider-thumb]:h-5 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-white [&::-webkit-slider-thumb]:shadow-[0_0_10px_rgba(255,255,255,0.5)]"
                  />
                  <div className="w-10 h-8 flex items-center justify-center bg-white/10 rounded-lg border border-white/10 font-mono text-[13px] font-semibold text-white">
                    {maxSteps}
                  </div>
                </div>
                <p className="text-[11px] text-white/40 mt-4 leading-relaxed tracking-wide">
                  Boundaries the agent's autonomy by strictly limiting the maximum sequential tasks taking per activation.
                </p>
              </div>

              <div className="bg-black/30 backdrop-blur-xl border border-white/10 rounded-[1.25rem] p-5 shadow-inner">
                <label className="flex items-center gap-2 text-[12px] font-semibold text-white mb-4">
                  <div className="p-1 rounded bg-purple-500/20 text-purple-400"><Settings size={14} /></div>
                  Automation Server Protocol
                </label>
                <input
                  type="text" value={serverUrl} onChange={(e) => setServerUrl(e.target.value)} onBlur={saveServerUrl} onKeyDown={(e) => { if (e.key === 'Enter') { e.target.blur(); saveServerUrl(e); } }}
                  placeholder="ws://localhost:8000"
                  className="w-full px-4 py-3 bg-black/40 border border-white/10 rounded-xl text-white font-mono text-xs focus:outline-none focus:border-white/30 focus:bg-black/60 transition-all shadow-inner"
                />
                <p className="text-[11px] text-white/40 mt-4 leading-relaxed tracking-wide">
                  Direct WebSocket channel linking the chromium environment to the external intelligent engine.
                </p>
              </div>

            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default App;
