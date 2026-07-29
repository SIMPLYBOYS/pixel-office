// WebGL → Web 外殼的單向通知：點畫布裡的 NPC 時把員工 id 送給外層頁（/shell）。
// 只送 id，不送任何內容——外殼收到自己去抓 /office/report，維持單一資料權威。
mergeInto(LibraryManager.library, {
  OfficeSelectAgent: function (idPtr) {
    var id = UTF8ToString(idPtr);
    if (window.parent && window.parent !== window) {
      window.parent.postMessage({ source: 'office-canvas', agent: id }, window.location.origin);
    }
  },
});
