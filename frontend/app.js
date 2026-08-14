// Simple UI script to check API health
document.getElementById('checkBtn').addEventListener('click', async () => {
  const resultEl = document.getElementById('result');
  resultEl.textContent = 'Checking...';
  try {
    const resp = await fetch('/api/v1/health');
    const data = await resp.json();
    resultEl.textContent = JSON.stringify(data, null, 2);
  } catch (err) {
    resultEl.textContent = `Error: ${err.message}`;
  }
});
