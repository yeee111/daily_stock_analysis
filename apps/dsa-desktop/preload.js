const { contextBridge, ipcRenderer } = require('electron');

const DESKTOP_VERSION_ARG_PREFIX = '--dsa-desktop-version=';
const DESKTOP_UPDATE_CHECK_ARG_PREFIX = '--dsa-desktop-update-check=';
const DESKTOP_GET_UPDATE_STATE_CHANNEL = 'desktop:get-update-state';
const DESKTOP_CHECK_FOR_UPDATES_CHANNEL = 'desktop:check-for-updates';
const DESKTOP_INSTALL_DOWNLOADED_UPDATE_CHANNEL = 'desktop:install-downloaded-update';
const DESKTOP_OPEN_RELEASE_PAGE_CHANNEL = 'desktop:open-release-page';
const DESKTOP_RENDER_SHARE_IMAGE_CHANNEL = 'desktop:render-share-image';
const DESKTOP_UPDATE_STATE_EVENT = 'desktop:update-state';

function readDesktopVersion(argv = process.argv) {
  const versionArg = argv.find(
    (value) => typeof value === 'string' && value.startsWith(DESKTOP_VERSION_ARG_PREFIX)
  );
  return versionArg ? versionArg.slice(DESKTOP_VERSION_ARG_PREFIX.length) : '';
}

function readUpdateCheckEnabled(argv = process.argv) {
  const updateArg = argv.find(
    (value) => typeof value === 'string' && value.startsWith(DESKTOP_UPDATE_CHECK_ARG_PREFIX)
  );
  if (!updateArg) {
    return true;
  }
  return updateArg.slice(DESKTOP_UPDATE_CHECK_ARG_PREFIX.length) !== '0';
}

function createDesktopBridge({
  version = readDesktopVersion(),
  renderer = ipcRenderer,
  updateCheckEnabled = readUpdateCheckEnabled(),
} = {}) {
  const bridge = {
    version,
    openReleasePage(releaseUrl) {
      return renderer.invoke(DESKTOP_OPEN_RELEASE_PAGE_CHANNEL, releaseUrl);
    },
    renderShareImage(recordId) {
      return renderer.invoke(DESKTOP_RENDER_SHARE_IMAGE_CHANNEL, recordId);
    },
  };

  // Local builds disable update checks; omitting the update bridge keeps the
  // Web UI from rendering update entries that can never succeed.
  if (!updateCheckEnabled) {
    return bridge;
  }

  return {
    ...bridge,
    getUpdateState() {
      return renderer.invoke(DESKTOP_GET_UPDATE_STATE_CHANNEL);
    },
    checkForUpdates() {
      return renderer.invoke(DESKTOP_CHECK_FOR_UPDATES_CHANNEL);
    },
    installDownloadedUpdate() {
      return renderer.invoke(DESKTOP_INSTALL_DOWNLOADED_UPDATE_CHANNEL);
    },
    onUpdateStateChange(listener) {
      if (typeof listener !== 'function') {
        return () => undefined;
      }

      const handler = (_event, payload) => {
        listener(payload);
      };
      renderer.on(DESKTOP_UPDATE_STATE_EVENT, handler);
      return () => {
        renderer.removeListener(DESKTOP_UPDATE_STATE_EVENT, handler);
      };
    },
  };
}

contextBridge.exposeInMainWorld('dsaDesktop', createDesktopBridge());

module.exports = {
  DESKTOP_CHECK_FOR_UPDATES_CHANNEL,
  DESKTOP_GET_UPDATE_STATE_CHANNEL,
  DESKTOP_INSTALL_DOWNLOADED_UPDATE_CHANNEL,
  DESKTOP_OPEN_RELEASE_PAGE_CHANNEL,
  DESKTOP_RENDER_SHARE_IMAGE_CHANNEL,
  DESKTOP_UPDATE_CHECK_ARG_PREFIX,
  DESKTOP_UPDATE_STATE_EVENT,
  DESKTOP_VERSION_ARG_PREFIX,
  createDesktopBridge,
  readDesktopVersion,
  readUpdateCheckEnabled,
};
