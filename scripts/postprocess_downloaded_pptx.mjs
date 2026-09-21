#!/usr/bin/env node
/**
 * Post-process a downloaded NotebookLM slide deck:
 * - cover the fixed lower-right watermark area with a same-color rectangle
 * - add the local default logo image near the upper-right corner
 * - export a user-facing PPTX whose name ends with "_水印版.pptx"
 */

import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_IMAGE_PATH = path.resolve(SCRIPT_DIR, "..", "assets", "logo.png");

const DEFAULTS = {
  watermarkRectCm: {
    left: 41.63,
    top: 24.55,
    width: 3.39,
    height: 0.64,
  },
  logoRectCm: {
    left: 40.26,
    top: 23.36,
    width: 4.10,
    height: 1.19,
  },
  logoScalePercent: {
    width: 6,
    height: 6,
  },
  suffix: "_水印版",
  imagePath: DEFAULT_IMAGE_PATH,
};

function parseArgs(argv) {
  const args = {
    input: "",
    output: "",
    imagePath: DEFAULTS.imagePath,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--input") {
      args.input = argv[++index] || "";
    } else if (token === "--output") {
      args.output = argv[++index] || "";
    } else if (token === "--image-path") {
      args.imagePath = argv[++index] || "";
    } else if (token === "--help" || token === "-h") {
      printHelp();
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${token}`);
    }
  }
  if (!args.input || !args.output) {
    throw new Error("Both --input and --output are required.");
  }
  return args;
}

function printHelp() {
  process.stdout.write(
    [
      "Usage: postprocess_downloaded_pptx.mjs --input <raw.pptx> --output <final_水印版.pptx> [--image-path <logo.png>]",
      "",
      "Applies the local default PPT post-processing rules to a downloaded deck.",
      "",
    ].join("\n"),
  );
}

function cmToPx(cm) {
  return (cm / 2.54) * 96;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function rgbToHex(r, g, b) {
  return (
    "#" +
    [r, g, b]
      .map((value) => Math.round(clamp(value, 0, 255)).toString(16).padStart(2, "0"))
      .join("")
      .toUpperCase()
  );
}

function getPixel(png, x, y) {
  const xx = Math.max(0, Math.min(png.width - 1, Math.round(x)));
  const yy = Math.max(0, Math.min(png.height - 1, Math.round(y)));
  const idx = (png.width * yy + xx) << 2;
  return {
    r: png.data[idx],
    g: png.data[idx + 1],
    b: png.data[idx + 2],
    a: png.data[idx + 3],
  };
}

function pushRegionSamples(png, out, x0, y0, x1, y1) {
  const left = Math.max(0, Math.floor(Math.min(x0, x1)));
  const top = Math.max(0, Math.floor(Math.min(y0, y1)));
  const right = Math.min(png.width - 1, Math.ceil(Math.max(x0, x1)));
  const bottom = Math.min(png.height - 1, Math.ceil(Math.max(y0, y1)));
  for (let y = top; y <= bottom; y += 1) {
    for (let x = left; x <= right; x += 1) {
      const pixel = getPixel(png, x, y);
      if (pixel.a !== 0) {
        out.push(pixel);
      }
    }
  }
}

function pickBackgroundColor(png, rect) {
  const samples = [];
  const margin = 10;
  pushRegionSamples(png, samples, rect.left - margin, rect.top, rect.left - 2, rect.top + rect.height);
  pushRegionSamples(png, samples, rect.left, rect.top - margin, rect.left + rect.width, rect.top - 2);
  pushRegionSamples(
    png,
    samples,
    rect.left + rect.width + 2,
    rect.top,
    rect.left + rect.width + margin,
    rect.top + rect.height,
  );
  pushRegionSamples(
    png,
    samples,
    rect.left,
    rect.top + rect.height + 2,
    rect.left + rect.width,
    rect.top + rect.height + margin,
  );

  if (samples.length === 0) {
    const pixel = getPixel(png, rect.left - 4, rect.top - 4);
    return {
      hex: rgbToHex(pixel.r, pixel.g, pixel.b),
      sampleCount: 1,
      method: "fallback-single-pixel",
    };
  }

  const buckets = new Map();
  for (const pixel of samples) {
    const key = [Math.round(pixel.r / 8), Math.round(pixel.g / 8), Math.round(pixel.b / 8)].join("-");
    const bucket = buckets.get(key) || { count: 0, r: 0, g: 0, b: 0 };
    bucket.count += 1;
    bucket.r += pixel.r;
    bucket.g += pixel.g;
    bucket.b += pixel.b;
    buckets.set(key, bucket);
  }

  let bestKey = "";
  let bestBucket = null;
  for (const [key, bucket] of buckets.entries()) {
    if (!bestBucket || bucket.count > bestBucket.count) {
      bestKey = key;
      bestBucket = bucket;
    }
  }

  return {
    hex: rgbToHex(bestBucket.r / bestBucket.count, bestBucket.g / bestBucket.count, bestBucket.b / bestBucket.count),
    sampleCount: samples.length,
    method: "dominant-neighbor-bucket",
    bucket: bestKey,
  };
}

async function saveBlob(targetPath, blob) {
  await fs.writeFile(targetPath, new Uint8Array(await blob.arrayBuffer()));
}

async function resolveNodeModulesDir() {
  const candidates = [];
  if (process.env.CODEX_NODE_MODULES) {
    candidates.push(process.env.CODEX_NODE_MODULES);
  }
  if (process.env.NODE_PATH) {
    candidates.push(...process.env.NODE_PATH.split(path.delimiter).filter(Boolean));
  }
  candidates.push(path.join(os.homedir(), ".cache", "codex-runtimes", "codex-primary-runtime", "dependencies", "node", "node_modules"));

  const cacheRoot = path.join(os.homedir(), ".cache", "codex-runtimes");
  try {
    const runtimeEntries = await fs.readdir(cacheRoot, { withFileTypes: true });
    for (const entry of runtimeEntries) {
      if (entry.isDirectory()) {
        candidates.push(path.join(cacheRoot, entry.name, "dependencies", "node", "node_modules"));
      }
    }
  } catch {
    // Ignore missing cache root; the explicit candidates above may still work.
  }

  for (const candidate of candidates) {
    if (!candidate) {
      continue;
    }
    try {
      await fs.access(path.join(candidate, "@oai", "artifact-tool", "dist", "artifact_tool.mjs"));
      return candidate;
    } catch {
      // Keep scanning.
    }
  }
  throw new Error("Could not find a node_modules directory containing @oai/artifact-tool.");
}

async function loadRuntimeModules() {
  const nodeModulesDir = await resolveNodeModulesDir();
  const helperRequire = createRequire(path.join(nodeModulesDir, "__codex__.cjs"));
  const artifactEntry = helperRequire.resolve("@oai/artifact-tool");
  const artifact = await import(pathToFileURL(artifactEntry).href);
  const { PNG } = helperRequire("pngjs");
  return { artifact, PNG, nodeModulesDir };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const inputPath = path.resolve(args.input);
  const outputPath = path.resolve(args.output);
  const imagePath = path.resolve(args.imagePath || DEFAULTS.imagePath);

  await fs.access(inputPath);
  await fs.access(imagePath);
  await fs.mkdir(path.dirname(outputPath), { recursive: true });

  const { artifact, PNG, nodeModulesDir } = await loadRuntimeModules();
  const imageBytes = await fs.readFile(imagePath);
  const presentation = await artifact.PresentationFile.importPptx(await artifact.FileBlob.load(inputPath));

  const watermarkRect = {
    left: cmToPx(DEFAULTS.watermarkRectCm.left),
    top: cmToPx(DEFAULTS.watermarkRectCm.top),
    width: cmToPx(DEFAULTS.watermarkRectCm.width),
    height: cmToPx(DEFAULTS.watermarkRectCm.height),
  };
  const logoRect = {
    left: cmToPx(DEFAULTS.logoRectCm.left),
    top: cmToPx(DEFAULTS.logoRectCm.top),
    width: cmToPx(DEFAULTS.logoRectCm.width),
    height: cmToPx(DEFAULTS.logoRectCm.height),
  };

  const perSlide = [];
  for (let index = 0; index < presentation.slides.items.length; index += 1) {
    const slide = presentation.slides.items[index];
    const beforeBlob = await presentation.export({ slide, format: "png", scale: 1 });
    const beforePath = path.join(os.tmpdir(), `nlm-course-slides-postprocess-${process.pid}-${index + 1}.png`);
    await saveBlob(beforePath, beforeBlob);
    const beforePng = PNG.sync.read(Buffer.from(await fs.readFile(beforePath)));
    await fs.unlink(beforePath).catch(() => {});

    const colorInfo = pickBackgroundColor(beforePng, watermarkRect);
    const mask = slide.shapes.add({
      geometry: "rect",
      name: `codex-watermark-mask-${String(index + 1).padStart(2, "0")}`,
      position: watermarkRect,
      fill: { type: "solid", color: colorInfo.hex },
      line: { style: "solid", fill: "none", width: 0 },
    });
    mask.opacity = 1;

    const image = slide.images.add({
      blob: imageBytes,
      contentType: "image/png",
      alt: "红杉智汇标识",
      position: logoRect,
    });
    image.lockAspectRatio = true;

    perSlide.push({
      slide: index + 1,
      sampledColor: colorInfo.hex,
      sampleCount: colorInfo.sampleCount,
      method: colorInfo.method,
    });
  }

  const colorCounts = {};
  for (const item of perSlide) {
    colorCounts[item.sampledColor] = (colorCounts[item.sampledColor] || 0) + 1;
  }
  const nonWhiteSlides = perSlide.filter((item) => item.sampledColor !== "#FFFFFF");

  const pptx = await artifact.PresentationFile.exportPptx(presentation);
  await pptx.save(outputPath);
  await fs.unlink(`${outputPath}.inspect.ndjson`).catch(() => {});

  process.stdout.write(
    `${JSON.stringify(
      {
        input_path: inputPath,
        output_path: outputPath,
        image_path: imagePath,
        node_modules_dir: nodeModulesDir,
        slide_count: perSlide.length,
        suffix: DEFAULTS.suffix,
        logo_rect_cm: DEFAULTS.logoRectCm,
        logo_scale_percent: DEFAULTS.logoScalePercent,
        logo_lock_aspect_ratio: true,
        logo_relative_to_original_size: true,
        color_counts: colorCounts,
        non_white_slides: nonWhiteSlides,
      },
      null,
      2,
    )}\n`,
  );
}

main().catch((error) => {
  process.stderr.write(`${String(error?.stack || error)}\n`);
  process.exitCode = 1;
});
