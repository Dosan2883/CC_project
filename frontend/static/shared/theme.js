try {
  const theme = localStorage.getItem("theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
} catch { /* 저장소를 쓸 수 없으면 시스템 테마를 따른다. */ }
