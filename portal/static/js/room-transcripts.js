// Admin room transcripts (#904): fetch stored transcripts for a channel and
// render them safely. The previous inline script interpolated transcript text
// into an HTML string, so stored text could inject markup into the admin page.

const container = document.getElementById('transcript-viewer-container');
const select = document.getElementById('transcript_lang_select');
const refreshBtn = document.getElementById('refresh-transcripts-btn');
const downloadBtn = document.getElementById('download-csv-btn');

const eventId = container?.dataset.eventId;
const roomId = container?.dataset.roomId;

let currentLanguageCode = '';
let downloadUrl = '';
// Monotonic id for in-flight requests, so a slow response for a previously
// selected channel cannot overwrite the table or the CSV of the current one.
let requestSequence = 0;

function setPlaceholder(text, className = 'muted transcript-placeholder') {
  if (!container) return;
  const placeholder = document.createElement('p');
  placeholder.className = className;
  placeholder.textContent = text;
  container.replaceChildren(placeholder);
}

function transcriptsUrl(languageCode) {
  return `/api/admin/events/${eventId}/rooms/${roomId}/transcripts/${languageCode}`;
}

function renderTable(rows) {
  if (!container) return;

  const table = document.createElement('table');
  table.className = 'data-table transcript-table';

  const headRow = document.createElement('tr');
  const timeHeader = document.createElement('th');
  timeHeader.className = 'transcript-time-col';
  timeHeader.textContent = 'Time';
  const textHeader = document.createElement('th');
  textHeader.textContent = 'Text';
  headRow.append(timeHeader, textHeader);

  const head = document.createElement('thead');
  head.append(headRow);

  const body = document.createElement('tbody');
  for (const row of rows) {
    const tr = document.createElement('tr');

    const timeCell = document.createElement('td');
    timeCell.className = 'muted';
    timeCell.textContent = new Date(row.created_at).toLocaleTimeString();

    const textCell = document.createElement('td');
    // textContent keeps stored transcript text inert, even if it contains HTML.
    textCell.textContent = row.text;

    tr.append(timeCell, textCell);
    body.append(tr);
  }

  table.append(head, body);
  container.replaceChildren(table);
}

function csvCell(value) {
  let text = String(value ?? '');
  // Spreadsheet apps evaluate cells starting with these prefixes even inside quotes.
  if (/^[=+\-@\t\r]/.test(text)) {
    text = `'${text}`;
  }
  return `"${text.replace(/"/g, '""')}"`;
}

function clearDownload() {
  if (downloadUrl) {
    URL.revokeObjectURL(downloadUrl);
    downloadUrl = '';
  }
  downloadBtn.hidden = true;
  downloadBtn.removeAttribute('href');
}

function updateCsvDownload(rows, languageCode) {
  if (!downloadBtn) return;

  const lines = ['Time,Text', ...rows.map((row) => `${csvCell(row.created_at)},${csvCell(row.text)}`)];
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });

  if (downloadUrl) URL.revokeObjectURL(downloadUrl);
  downloadUrl = URL.createObjectURL(blob);

  downloadBtn.href = downloadUrl;
  // Name the file after the channel the rows came from, not the current selection.
  downloadBtn.download = `transcripts_${languageCode.replace(/[^a-zA-Z0-9_-]/g, '_')}.csv`;
  downloadBtn.hidden = false;
}

async function fetchTranscripts() {
  if (!currentLanguageCode || !container) return;

  const languageCode = currentLanguageCode;
  const requestId = ++requestSequence;
  const isCurrentRequest = () => requestId === requestSequence && languageCode === currentLanguageCode;

  setPlaceholder('Loading...');
  // A previous channel's CSV must not stay downloadable while this one loads.
  clearDownload();

  try {
    const response = await fetch(transcriptsUrl(languageCode));
    if (!response.ok) {
      throw new Error(`Failed to fetch transcripts (${response.status})`);
    }

    const rows = await response.json();
    if (!isCurrentRequest()) return;
    if (!Array.isArray(rows) || rows.length === 0) {
      setPlaceholder('No transcripts recorded for this channel yet.');
      clearDownload();
      return;
    }

    renderTable(rows);
    updateCsvDownload(rows, languageCode);
    container.scrollTop = container.scrollHeight;
  } catch (error) {
    if (!isCurrentRequest()) return;
    console.error('Failed to load room transcripts', error);
    setPlaceholder(`Error: ${error.message}`, 'danger-text transcript-placeholder');
    clearDownload();
  }
}

if (container && select && refreshBtn && downloadBtn) {
  select.addEventListener('change', (event) => {
    currentLanguageCode = event.target.value;
    if (currentLanguageCode) {
      fetchTranscripts();
    } else {
      setPlaceholder('Select a channel above to view its transcripts.');
      clearDownload();
    }
  });

  refreshBtn.addEventListener('click', fetchTranscripts);
} else {
  console.error('Room transcript viewer is missing required elements');
}
