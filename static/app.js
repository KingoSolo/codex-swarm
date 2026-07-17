const STATUSES = [
  { key: "todo", label: "Todo" },
  { key: "in_progress", label: "In Progress" },
  { key: "done", label: "Done" },
];

const lists = Object.fromEntries(
  STATUSES.map(({ key }) => [key, document.querySelector(`#${key.replace("_", "-")}-tasks`)]),
);
const counts = Object.fromEntries(
  STATUSES.map(({ key }) => [key, document.querySelector(`#${key.replace("_", "-")}-count`)]),
);
const notice = document.querySelector("#notice");
const createForm = document.querySelector("#create-form");
const editDialog = document.querySelector("#edit-dialog");
const editForm = document.querySelector("#edit-form");
let tasks = [];
let editingTaskId = null;

function showNotice(message, type = "error") {
  notice.textContent = message;
  notice.className = `notice ${type}`;
  notice.hidden = false;
}

function clearNotice() {
  notice.hidden = true;
  notice.textContent = "";
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || "Something went wrong. Please try again.");
  }
  return response.status === 204 ? null : response.json();
}

function statusOptions(current) {
  return STATUSES.map(({ key, label }) => {
    const option = document.createElement("option");
    option.value = key;
    option.textContent = label;
    option.selected = key === current;
    return option;
  });
}

function createCard(task) {
  const card = document.createElement("article");
  card.className = "task-card";
  card.dataset.taskId = task.id;

  const title = document.createElement("h3");
  title.textContent = task.title;
  card.append(title);

  if (task.description) {
    const description = document.createElement("p");
    description.className = "task-description";
    description.textContent = task.description;
    card.append(description);
  }

  const controls = document.createElement("div");
  controls.className = "task-controls";
  const select = document.createElement("select");
  select.className = "status-select";
  select.setAttribute("aria-label", `Move ${task.title}`);
  statusOptions(task.status).forEach((option) => select.append(option));
  controls.append(select);

  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "text-button";
  edit.dataset.action = "edit";
  edit.textContent = "Edit";
  controls.append(edit);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "text-button danger";
  remove.dataset.action = "delete";
  remove.textContent = "Delete";
  controls.append(remove);
  card.append(controls);
  return card;
}

function render() {
  const grouped = Object.fromEntries(STATUSES.map(({ key }) => [key, []]));
  tasks.forEach((task) => grouped[task.status]?.push(task));
  STATUSES.forEach(({ key, label }) => {
    const list = lists[key];
    list.replaceChildren();
    counts[key].textContent = grouped[key].length;
    if (!grouped[key].length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = `No tasks ${label.toLowerCase()}.`;
      list.append(empty);
      return;
    }
    grouped[key].forEach((task) => list.append(createCard(task)));
  });
}

async function loadTasks() {
  clearNotice();
  try {
    tasks = await request("/api/tasks");
    render();
  } catch (error) {
    showNotice(error.message);
  }
}

createForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = createForm.querySelector('[type="submit"]');
  const title = document.querySelector("#new-title").value.trim();
  if (!title) return showNotice("A task title is required.");
  submit.disabled = true;
  try {
    await request("/api/tasks", {
      method: "POST",
      body: JSON.stringify({ title, description: document.querySelector("#new-description").value }),
    });
    createForm.reset();
    await loadTasks();
  } catch (error) {
    showNotice(error.message);
  } finally {
    submit.disabled = false;
  }
});

document.querySelector(".board").addEventListener("change", async (event) => {
  if (!event.target.matches(".status-select")) return;
  const taskId = event.target.closest(".task-card").dataset.taskId;
  event.target.disabled = true;
  try {
    await request(`/api/tasks/${taskId}`, { method: "PATCH", body: JSON.stringify({ status: event.target.value }) });
    await loadTasks();
  } catch (error) {
    showNotice(error.message);
    await loadTasks();
  }
});

document.querySelector(".board").addEventListener("click", async (event) => {
  const action = event.target.dataset.action;
  if (!action) return;
  const taskId = Number(event.target.closest(".task-card").dataset.taskId);
  const task = tasks.find((item) => item.id === taskId);
  if (!task) return;
  if (action === "edit") {
    editingTaskId = taskId;
    document.querySelector("#edit-title").value = task.title;
    document.querySelector("#edit-description").value = task.description;
    editDialog.showModal();
  }
  if (action === "delete" && window.confirm(`Delete “${task.title}”?`)) {
    try {
      await request(`/api/tasks/${taskId}`, { method: "DELETE" });
      await loadTasks();
    } catch (error) {
      showNotice(error.message);
    }
  }
});

editForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = document.querySelector("#edit-title").value.trim();
  if (!title) return showNotice("A task title is required.");
  const submit = editForm.querySelector('[type="submit"]');
  submit.disabled = true;
  try {
    await request(`/api/tasks/${editingTaskId}`, {
      method: "PATCH",
      body: JSON.stringify({ title, description: document.querySelector("#edit-description").value }),
    });
    editDialog.close();
    await loadTasks();
  } catch (error) {
    showNotice(error.message);
  } finally {
    submit.disabled = false;
  }
});

document.querySelectorAll("[data-close-dialog]").forEach((button) => {
  button.addEventListener("click", () => editDialog.close());
});

loadTasks();
