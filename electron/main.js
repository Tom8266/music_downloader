const { app, BrowserWindow, dialog, ipcMain } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const http = require('http');

let mainWindow;
let serverProcess;
const SERVER_PORT = 8080;

function getAppPath(...segments) {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'app', ...segments);
  }
  return path.join(__dirname, '..', ...segments);
}

function getRootPath(...segments) {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, ...segments);
  }
  return path.join(__dirname, '..', ...segments);
}

ipcMain.handle('select-directory', async () => {
  if (!mainWindow) return { canceled: true };
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ['openDirectory'],
    title: '选择下载目录',
  });
  return result;
});

function findPython() {
  const base = app.isPackaged ? process.resourcesPath : path.join(__dirname, '..');
  if (process.platform === 'win32') {
    return path.join(base, '.venv', 'Scripts', 'python.exe');
  }
  return path.join(base, '.venv', 'bin', 'python3');
}

function startServer() {
  const python = findPython();
  const webuiPath = getAppPath('webui.py');

  serverProcess = spawn(python, [
    webuiPath, '--host', '127.0.0.1', '--port', String(SERVER_PORT),
  ], {
    cwd: getRootPath(),
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
  });

  serverProcess.stderr.on('data', (data) => {
    console.log('[server]', data.toString().trim());
  });

  serverProcess.on('error', (err) => {
    dialog.showErrorBox('启动失败', `无法启动后端服务:\n${err.message}`);
    app.quit();
  });

  return new Promise((resolve, reject) => {
    const startTime = Date.now();
    const check = () => {
      http.get(`http://127.0.0.1:${SERVER_PORT}/`, (res) => {
        resolve();
      }).on('error', () => {
        if (Date.now() - startTime > 15000) {
          reject(new Error('后端启动超时'));
          return;
        }
        setTimeout(check, 300);
      });
    };
    setTimeout(check, 500);
  });
}

function createWindow() {
  const iconPath = app.isPackaged
    ? path.join(process.resourcesPath, 'electron', 'icon.png')
    : path.join(__dirname, 'icon.png');

  mainWindow = new BrowserWindow({
    width: 1100,
    height: 750,
    minWidth: 800,
    minHeight: 600,
    title: 'Music Downloader',
    icon: iconPath,
    backgroundColor: '#0f0f1a',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  mainWindow.loadURL(`http://127.0.0.1:${SERVER_PORT}/`);
  mainWindow.setMenuBarVisibility(false);

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

app.whenReady().then(async () => {
  try {
    await startServer();
    createWindow();
  } catch (err) {
    dialog.showErrorBox('启动失败', err.message);
    app.quit();
  }
});

app.on('window-all-closed', () => {
  if (serverProcess) serverProcess.kill();
  app.quit();
});

app.on('before-quit', () => {
  if (serverProcess) serverProcess.kill();
});
