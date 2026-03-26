// Prevent Dropzone from auto-discovering forms before we configure it
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

// Upload a single File object with a given prefix (folder path) to the volume upload endpoint
function uploadFileWithPrefix(file, uploadUrl, prefix) {
    var url = uploadUrl.split('?')[0];
    var basePrefix = new URLSearchParams(uploadUrl.split('?')[1] || '').get('prefix') || '';
    var fullPrefix = prefix ? (basePrefix.replace(/\/$/, '') + '/' + prefix).replace(/\/+/g, '/') : basePrefix;
    var formData = new FormData();
    formData.append('file', file, file.name);
    formData.append('csrfmiddlewaretoken', getCookie('csrftoken'));
    return fetch(url + '?prefix=' + encodeURIComponent(fullPrefix), {
        method: 'POST',
        headers: { 'X-CSRFToken': getCookie('csrftoken') },
        body: formData
    }).then(function(r) { return r.json(); }).then(function(response) {
        var blobs = Array.isArray(response) ? response : [];
        dzPostUploadToast(file.name, true);
        blobs.forEach(function(blob) { dzAppendFileRow(file, blob); });
    }).catch(function(err) {
        dzPostUploadToast(file.name + ': ' + err, false);
    });
}

// Recursively read a FileSystemDirectoryEntry and collect all files with their relative paths
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
                            results.push({ file: file, prefix: pathPrefix || '' });
                            if (--pending === 0) readBatch();
                        });
                    } else if (entry.isDirectory) {
                        readDirectoryEntries(entry, entryPath).then(function(subResults) {
                            results = results.concat(subResults);
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

$(document).ready(function () {
    if ($('.dz').length === 0) return;

    var uploadUrl = $('.dz').attr('action');

    // Folder upload via button
    $('#folder-upload-input').on('change', function() {
        var files = Array.from(this.files);
        files.forEach(function(file) {
            // webkitRelativePath is "folderName/sub/file.txt" — use dirname as prefix
            var rel = file.webkitRelativePath || file.name;
            var prefix = rel.includes('/') ? rel.substring(0, rel.lastIndexOf('/')) : '';
            uploadFileWithPrefix(file, uploadUrl, prefix);
        });
        this.value = '';
    });

    $('.dz').dropzone({
        previewTemplate: '<div style="display:none"></div>',
        maxFilesize: null,
        // Intercept drop to handle folders via FileSystem API
        drop: function(e) {
            var items = e.dataTransfer && e.dataTransfer.items;
            if (!items) return;
            var self = this;
            var hasFolder = false;
            Array.from(items).forEach(function(item) {
                var entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
                if (entry && entry.isDirectory) {
                    hasFolder = true;
                    readDirectoryEntries(entry, entry.name).then(function(fileEntries) {
                        fileEntries.forEach(function(fe) {
                            uploadFileWithPrefix(fe.file, uploadUrl, fe.prefix);
                        });
                    });
                }
            });
            // If no folders, let Dropzone handle it normally
            if (!hasFolder) {
                Dropzone.prototype.drop.call(self, e);
            }
        },
        init: function () {
            this.on('error', function (file, errorMessage) {
                dzPostUploadToast(errorMessage.detail || errorMessage, false);
            });
            this.on('success', function (file, response) {
                dzPostUploadToast(file.name, true);
                var blobs = Array.isArray(response) ? response : [];
                if (blobs.length > 0) {
                    blobs.forEach(function (blob) { dzAppendFileRow(file, blob); });
                } else {
                    dzAppendFileRow(file, null);
                }
            });
        }
    });
});

$('[data-api-url]').on('click', (e) => {
    $(e.currentTarget).prepend(`<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>`);
    let headers = new Headers();
    headers.append('X-CSRFToken', getCookie('csrftoken'));
    let request = new Request($(e.currentTarget).data('api-url'));
    fetch(request, {
        method: 'POST',
        headers: headers
    }).then(response => {
        $(e.currentTarget).find('span.spinner-border').remove();
    });
});

function dzAppendFileRow(file, blob) {
    const size = file.size > 0 ? (file.size / 1024).toFixed(1) + ' KB' : '—';
    var nameCell, actionsCell;
    if (blob && blob.uuid) {
        var uid = blob.uuid.replace(/-/g, '');
        var base = '/_/ui/core/blob/';
        nameCell = `<a href="${base}${blob.uuid}/"><i class="fa fa-file" aria-hidden="true"></i> ${file.name}</a>`;
        actionsCell = `
            <a class="btn btn-sm btn-primary" href="${base}${blob.uuid}/download/"><i class="fa fa-download text-light" aria-hidden="true"></i></a>
            <a class="btn btn-sm btn-primary" href="${base}${blob.uuid}/update/"><i class="fa fa-pencil text-light" aria-hidden="true"></i></a>
            <a class="btn btn-sm btn-danger" href="${base}${blob.uuid}/delete/"><i class="fa fa-trash text-light" aria-hidden="true"></i></a>`;
    } else {
        nameCell = `<i class="fa fa-file" aria-hidden="true"></i> ${file.name}`;
        actionsCell = '—';
    }
    const row = `<tr><td>${nameCell}</td><td>${size}</td><td>${actionsCell}</td></tr>`;
    $('#blob-table-body').append(row);
}

function dzPostUploadToast(message, uploadSucceeded) {
    const text = uploadSucceeded
        ? `Successfully uploaded ${message}.`
        : `Failed to upload file: ${message}.`;
    const template = `
        <div class="toast ml-auto" role="alert" data-delay="3000" data-autohide="true">
          <div class="toast-header">
            <strong class="mr-auto text-primary">Blob Upload</strong>
            <button type="button" class="ml-2 mb-1 close" data-dismiss="toast" aria-label="Close">
              <span aria-hidden="true">&times;</span>
            </button>
          </div>
          <div class="toast-body">${text}</div>
        </div>`;
    $('.alert-container').append(template);
    $('.toast').toast('show');
}
