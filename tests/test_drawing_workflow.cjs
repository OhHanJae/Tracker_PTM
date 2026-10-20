const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../pt503_tester/web/app.js'), 'utf8');
const recipeLoading = source.slice(source.indexOf('async function loadRecipes(select)'), source.indexOf('function selected()'));
const workflow = source.slice(source.indexOf('function compatibleDrawingRecipes()'), source.indexOf('function renderDrawingPointSets()'));
const moveControls = source.slice(source.indexOf('function updateDrawingMoveControls()'), source.indexOf('async function moveSelectedDrawingPoint()'));
const calibrationButton = source.slice(source.indexOf('function updateDrawingCalibrationButton()'), source.indexOf('async function toggleCalibrationDrawingPoint()'));

function runtime() {
    const elements = new Map();
    const ctx = {
        commands: ['drawing.select_recipe'],
        activeDrawing: {id: 'drawing', product_id: 'product', selected_point_file: 'group.json',
            point_sets: [{}], selected_recipe_id: 'recipe', product_rotation_deg: 0, product_tilt_deg: 0,
            points: [{id: 'point', pan: 1, tilt: 2}], calibration: {points_used: 4}},
        recipes: [{id: 'recipe', name: 'outer'}, {id: 'other', name: 'other'}],
        selectedDrawingPointId: 'point', calls: [],
        Option: function(text, value) {this.text = text; this.value = value;},
        $(id) {
            if (!elements.has(id)) elements.set(id, {value: '', disabled: false, textContent: '',
                replaceChildren(...children) {this.children = children;}});
            return elements.get(id);
        },
        guarded: fn => fn,
        drawingAction: fn => fn,
        selectedDrawingPoint: () => ctx.activeDrawing?.points?.find(point => point.id === ctx.selectedDrawingPointId),
        loadDrawingPoseInputs() {}, async loadRecipes() {},
        renderDrawingPanel() {ctx.renderDrawingRecipeSelector();},
        async cmd(command) {ctx.calls.push(command); return {commands: ctx.commands};},
    };
    vm.createContext(ctx);
    vm.runInContext(workflow + moveControls + calibrationButton, ctx);
    return ctx;
}

(async () => {
    let ctx = runtime();
    ctx.renderDrawingRecipeSelector();
    for (const id of ['applyCurrentDrawingPoint', 'toggleCalibrationPoint', 'calibrateDrawing', 'exportDrawingRecipe']) {
        assert.equal(ctx.$(id).disabled, false, `${id} is enabled for a bound zero-pose recipe`);
    }
    assert.match(ctx.$('drawingWorkflowStatus').textContent, /연결 완료/);

    ctx = runtime();
    ctx.commands = ['drawing.get'];
    ctx.renderDrawingRecipeSelector();
    assert.match(ctx.$('drawingWorkflowStatus').textContent, /이전 버전/);
    assert.equal(ctx.$('drawingActiveRecipe').disabled, true);
    assert.equal(ctx.$('calibrateDrawing').disabled, true);
    await assert.rejects(ctx.ensureDrawingWorkflow(), /서버를 완전히 중지/);
    assert.deepEqual(ctx.calls, [], 'old server is detected before a mutating request');

    ctx = runtime();
    ctx.activeDrawing.selected_recipe_id = '';
    ctx.renderDrawingRecipeSelector();
    assert.match(ctx.$('drawingWorkflowStatus').textContent, /레시피를 선택/);
    assert.equal(ctx.$('applyCurrentDrawingPoint').disabled, true);
    assert.equal(ctx.$('toggleCalibrationPoint').disabled, true);

    ctx = runtime();
    ctx.$('drawingActiveRecipe').value = 'other';
    ctx.cmd = async () => {throw Error('selection failed');};
    await assert.rejects(ctx.$('drawingActiveRecipe').onchange(), /selection failed/);
    assert.equal(ctx.$('drawingActiveRecipe').value, 'recipe', 'failed selection restores actual recipe');
    assert.equal(ctx.activeDrawing.selected_recipe_id, 'recipe');

    ctx = runtime();
    ctx.$('drawingActiveRecipe').value = 'other';
    ctx.cmd = async () => ({drawing: {id: 'drawing', calibration: {points_used: 4}}});
    await assert.rejects(ctx.$('drawingActiveRecipe').onchange(), /연결 정보를 반환하지/);
    assert.equal(ctx.$('drawingActiveRecipe').value, 'recipe', 'incompatible response never appears bound');

    ctx = runtime();
    ctx.activeDrawing.product_rotation_deg = 25;
    ctx.renderDrawingRecipeSelector();
    assert.equal(ctx.$('applyCurrentDrawingPoint').disabled, true);
    assert.equal(ctx.$('calibrateDrawing').disabled, false, 'pose conversion remains available');
    assert.match(ctx.$('drawingWorkflowStatus').textContent, /0°\/0°/);

    ctx = runtime();
    ctx.selectedDrawingPointId = null;
    ctx.renderDrawingRecipeSelector();
    assert.equal(ctx.$('applyCurrentDrawingPoint').disabled, true);
    assert.equal(ctx.$('moveDrawingPoint').disabled, true);

    ctx = runtime();
    ctx.renderPoints = () => {};
    let listedRecipes = [{id: 'recipe', name: 'renamed'}, {id: 'other', name: 'other'}];
    ctx.cmd = async command => {
        assert.equal(command, 'recipe.list');
        return {recipes: listedRecipes};
    };
    vm.runInContext(recipeLoading, ctx);
    await ctx.loadRecipes('recipe');
    assert.equal(ctx.$('drawingActiveRecipe').value, 'recipe', 'rename preserves drawing selection');
    assert.equal(ctx.$('drawingActiveRecipe').children[1].text, 'renamed', 'rename refreshes drawing options');

    listedRecipes = [...listedRecipes, {id: 'new', name: 'new'},
        {id: 'unrelated', name: 'unrelated', drawing: {product_id: 'product', point_file: 'different.json'}}];
    await ctx.loadRecipes('new');
    assert.deepEqual(Array.from(ctx.$('drawingActiveRecipe').children, option => option.value),
        ['', 'recipe', 'other', 'new'], 'new recipes appear while another group stays filtered out');
    assert.equal(ctx.$('drawingActiveRecipe').value, 'recipe', 'editing another recipe preserves drawing selection');

    listedRecipes = listedRecipes.filter(recipe => recipe.id !== 'recipe');
    await ctx.loadRecipes();
    assert.equal(ctx.activeDrawing.selected_recipe_id, '', 'deleted drawing recipe is cleared');
    assert.equal(ctx.$('drawingActiveRecipe').value, '');
    assert.deepEqual(Array.from(ctx.$('drawingActiveRecipe').children, option => option.value), ['', 'other', 'new']);
    for (const id of ['applyCurrentDrawingPoint', 'calibrateDrawing', 'exportDrawingRecipe', 'moveDrawingPoint']) {
        assert.equal(ctx.$(id).disabled, true, `${id} is disabled after deleting the selected recipe`);
    }

    ctx.activeDrawing = null;
    await ctx.loadRecipes();
    assert.equal(ctx.$('drawingActiveRecipe').disabled, true, 'recipe refresh also works before loading a drawing');

    console.log('PASS: server compatibility, button state, recipe refresh, failed recipe selection, pose teaching and point selection');
})().catch(error => {console.error(error); process.exitCode = 1;});
