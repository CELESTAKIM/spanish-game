

// === CLOUD-FREE LANDSAT 8 TILE URL GENERATOR (KENYA 2013–2025) ===
// Author: CELESTAKIM018@gmail.com

// 1️⃣ Load Kenya boundary
var kenya = ee.FeatureCollection("FAO/GAUL_SIMPLIFIED_500m/2015/level0")
  .filter(ee.Filter.eq("ADM0_NAME", "Kenya"));
Map.centerObject(kenya, 6);
Map.addLayer(kenya, {color: "blue"}, "Kenya Boundary");

// 2️⃣ Cloud masking function using QA_PIXEL band
function maskL8Clouds(image) {
  var qa = image.select('QA_PIXEL');
  // Bits 3 and 4 represent cloud and cloud shadow respectively
  var cloud = qa.bitwiseAnd(1 << 3).eq(0);
  var shadow = qa.bitwiseAnd(1 << 4).eq(0);
  var mask = cloud.and(shadow);

  // Apply reflectance scaling to surface reflectance bands
  var scaled = image.select(['SR_B.*']).multiply(0.0000275).add(-0.2);
  return scaled.updateMask(mask)
               .copyProperties(image, image.propertyNames());
}

// 3️⃣ Visualization parameters for true color
var visParams = {
  bands: ['SR_B4', 'SR_B3', 'SR_B2'],
  min: 0.03,
  max: 0.3,
  gamma: 1.4
};

// 4️⃣ Function to generate yearly composite and print tile URL
function generateComposite(year) {
  var start = ee.Date.fromYMD(year, 1, 1);
  var end = ee.Date.fromYMD(year, 12, 31);

  var collection = ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
    .filterBounds(kenya)
    .filterDate(start, end)
    .map(maskL8Clouds);

  var composite = collection.median().clip(kenya);
  Map.addLayer(composite, visParams, 'Landsat 8 ' + year);

  var mapid = composite.getMap(visParams);
  print('🛰️ Landsat 8 Cloud-Free Tile URL for ' + year + ':', mapid.urlFormat);
}

// 5️⃣ Generate for all years (2013–current)
var currentYear = ee.Date(Date.now()).get('year').getInfo();
var years = [];
for (var y = 2013; y <= currentYear; y++) years.push(y);

// Run all years
years.forEach(generateComposite);
