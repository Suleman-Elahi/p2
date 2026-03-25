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

$(document).ready(function () {
    if ($('.dz').length === 0) return;

    $('.dz').dropzone({
        previewTemplate: '<div style="display:none"></div>',
        maxFilesize: null,
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
