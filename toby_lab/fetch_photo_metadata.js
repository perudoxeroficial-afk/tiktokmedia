const { Blob, File } = require("node:buffer");

if (typeof globalThis.Blob === "undefined") {
    globalThis.Blob = Blob;
}

if (typeof globalThis.File === "undefined") {
    globalThis.File = File;
}

const Tiktok = require("@tobyg74/tiktok-api-dl");

async function main() {
    const url = process.argv[2];

    if (!url) {
        console.log(JSON.stringify({ status: "error", error: 'Uso: node fetch_photo_metadata.js "https://vt.tiktok.com/..."' }));
        process.exit(1);
    }

    try {
        const result = await Tiktok.Downloader(url, { version: "v1" });
        console.log(JSON.stringify(result));
    } catch (error) {
        console.log(JSON.stringify({ status: "error", error: error?.message || String(error) }));
        process.exit(1);
    }
}

main();
