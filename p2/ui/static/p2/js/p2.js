Dropzone.autoDiscover = false;

function getCookie(name) {
    var cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        var cookies = document.cookie.split(';');
        for (var i = 0; i < cookies.length; i++) {
            var cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

// ─── AJAX table refresh ───────────────────────────────────────────────────────

function refreshBlobTable() {
    var listUrl = document.querySelector('.dz') && document.querySelector('.dz').dataset.listUrl;
    if (!listUrl) { return; }
    fetch(listUrl, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(function(r) { return r.text(); })
        .then(function(html) {
            var parser = new DOMParser();
            var doc = parser.parseFromString(html, 'text/html');
            var newBody = doc.getElementById('blob-table-body');
            var curBody = document.getElementById('blob-table-body');
            if (newBody && curBody) {
                curBody.innerHTML = newBody.innerHTML;
            }
        })
        .catch(function(e) { console.warn('Table refresh failed:', e); });
}

// ─── Single-file upload ───────────────────────────────────────────────────────

async function uploadFile(file, uploadUrl, relativePath) {
    var formData = new FormData();
    formData.append('file', file);
    formData.append('relativePath', relativePath);

    var response = await fetch(uploadUrl, {
        method: 'POST',
        headers: { 'X-CSRFToken': getCookie('csrftoken') },
        body: formData
    });

    if (!response.ok) {
        var text = '';
        try { text = await response.text(); } catch (_) {}
        throw new Error('HTTP ' + response.status + (text ? ': ' + text.slice(0, 120) : ''));
    }
    return await response.json();
}

// ─── Folder upload with single progress toast ─────────────────────────────────

async function uploadFolder(files, uploadUrl) {
    var total = files.length;
    var done = 0;
    var failed = 0;
    var toastId = 'folder-toast-' + Date.now();

    // Show a single progress toast
    var toastHtml =
        '<div class="toast ml-auto" id="' + toastId + '" role="status" data-autohide="false">' +
          '<div class="toast-header">' +
            '<strong class="mr-auto text-primary"><i class="fa fa-folder-open"></i> Uploading folder</strong>' +
            '<button type="button" class="ml-2 mb-1 close" data-dismiss="toast"><span>&times;</span></button>' +
          '</div>' +
          '<div class="toast-body">' +
            '<div class="progress mb-1"><div class="progress-bar" id="' + toastId + '-bar" style="width:0%"></div></div>' +
            '<small id="' + toastId + '-label">0 / ' + total + '</small>' +
          '</div>' +
        '</div>';
    document.querySelector('.alert-container').insertAdjacentHTML('beforeend', toastHtml);
    $('#' + toastId).toast('show');

    for (var i = 0; i < files.length; i++) {
        var fe = files[i];
        try {
            await uploadFile(fe.file, uploadUrl, fe.relativePath);
        } catch (e) {
            console.error('Upload failed for', fe.relativePath, e);
            failed++;
        }
        done++;
        var pct = Math.round((done / total) * 100);
        document.getElementById(toastId + '-bar').style.width = pct + '%';
        document.getElementById(toastId + '-label').textContent =
            done + ' / ' + total + (failed ? ' (' + failed + ' failed)' : '');
    }

    // Update toast to final state
    var bar = document.getElementById(toastId + '-bar');
    bar.classList.add(failed ? 'bg-warning' : 'bg-success');
    document.getElementById(toastId + '-label').textContent =
        failed
            ? (done - failed) + ' uploaded, ' + failed + ' failed'
            : total + ' files uploaded';

    // Auto-hide after 3s and refresh table
    setTimeout(function() { $('#' + toastId).toast('hide'); }, 3000);
    refreshBlobTable();
}

// ─── Directory reader ─────────────────────────────────────────────────────────

function readDirectoryEntries(dirEntry, pathPrefix) {
    return new Promise(function(resolve) {
        var results = [];
        var reader = dirEntry.createReader();
        function readBatch() {
            reader.readEntries(function(entries) {
                if (!entries.length) { resolve(results); return; }
                var pending = entries.length;
                entries.forEach(function(entry) {
                    var entryPath = pathPrefix ? pathPrefix + '/' + entry.name : entry.name;
                    if (entry.isFile) {
                        entry.file(function(file) {
                            results.push({ file: file, relativePath: entryPath });
                            if (--pending === 0) readBatch();
                        });
                    } else if (entry.isDirectory) {
                        readDirectoryEntries(entry, entryPath).then(function(sub) {
                            results = results.concat(sub);
                            if (--pending === 0) readBatch();
                        });
                    } else {
                        if (--pending === 0) readBatch();
                    }
                });
            });
        }
        readBatch();
    });
}

// ─── Init ─────────────────────────────────────────────────────────────────────

$(document).ready(function () {
    if ($('.dz').length === 0) return;

    var uploadUrl = $('.dz').data('upload-url');

    // Folder button
    $('#folder-upload-input').on('change', function() {
        var fileList = Array.from(this.files);
        var files = fileList.map(function(f) {
            return { file: f, relativePath: f.webkitRelativePath || f.name };
        });
        this.value = '';
        uploadFolder(files, uploadUrl);
    });

    $('.dz').dropzone({
        previewTemplate: '<div style="display:none"></div>',
        maxFilesize: null,
        drop: function(e) {
            var items = e.dataTransfer && e.dataTransfer.items;
            if (!items) return;
            var self = this;
            var folderFiles = [];
            var plainFiles = [];
            var pending = items.length;

            function checkDone() {
                if (--pending > 0) return;
                if (folderFiles.length) {
                    uploadFolder(folderFiles, uploadUrl);
                }
                // plain files handled by addedfile below
            }

            Array.from(items).forEach(function(item) {
                var entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
                if (entry && entry.isDirectory) {
                    readDirectoryEntries(entry, entry.name).then(function(sub) {
                        folderFiles = folderFiles.concat(sub);
                        checkDone();
                    });
                } else {
                    checkDone();
                }
            });

            // Let Dropzone handle plain files via addedfile
            Dropzone.prototype.drop.call(self, e);
        },
        init: function () {
            var self = this;
            this.on('addedfile', function(file) {
                self.removeFile(file);
                // Single file — upload and refresh
                uploadFile(file, uploadUrl, file.name)
                    .then(function(data) {
                        dzToast(file.name, true);
                        refreshBlobTable();
                    })
                    .catch(function(err) {
                        dzToast(file.name + ': ' + err.message, false);
                    });
            });
        }
    });
});

// ─── API action buttons ───────────────────────────────────────────────────────

$('[data-api-url]').on('click', function(e) {
    var btn = $(e.currentTarget);
    btn.prepend('<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>');
    fetch(btn.data('api-url'), {
        method: 'POST',
        headers: { 'X-CSRFToken': getCookie('csrftoken') }
    }).then(function() { btn.find('span.spinner-border').remove(); });
});

// ─── Toast helper ─────────────────────────────────────────────────────────────

function dzToast(message, ok) {
    var text = ok ? 'Uploaded ' + message : 'Failed: ' + message;
    var html =
        '<div class="toast ml-auto" role="alert" data-delay="4000" data-autohide="true">' +
          '<div class="toast-header">' +
            '<strong class="mr-auto ' + (ok ? 'text-success' : 'text-danger') + '">Upload</strong>' +
            '<button type="button" class="ml-2 mb-1 close" data-dismiss="toast"><span>&times;</span></button>' +
          '</div>' +
          '<div class="toast-body">' + text + '</div>' +
        '</div>';
    document.querySelector('.alert-container').insertAdjacentHTML('beforeend', html);
    $('.alert-container .toast').last().toast('show');
}
