/* Семёрочка frontend: vanilla HTML/CSS/JS, без сборщика и сторонних библиотек. */
(() => {
  const cfg = window.APP_CONFIG || {API_BASE_URL:"/api",ROUTES:{products:"/products",product:s=>`/products/${encodeURIComponent(s)}`,cart:"/cart",orders:"/orders",order:id=>`/orders/${encodeURIComponent(id)}`,notifications:"/notifications"},PAGE_SIZE:12,CURRENCY:"RUB"};
  const $ = (id) => document.getElementById(id);
  const state = {view:"catalog",products:[],cart:JSON.parse(localStorage.getItem("sem_cart")||"[]"),page:0,query:"",category:"",minPrice:"",maxPrice:"",token:localStorage.getItem("sem_token")||"",role:localStorage.getItem("sem_role")||"user",editing:null,checkoutKey:null};
  const money = n => `${Number(n||0).toLocaleString("ru-RU")} ₽`;
  const esc = s => String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const toast = msg => {const el=$("toast");el.textContent=msg;el.classList.add("show");setTimeout(()=>el.classList.remove("show"),2800)};
  const showNotice = (id,msg,error=false) => {const el=$(id);el.textContent=msg;el.classList.toggle("hidden",!msg);el.classList.toggle("error",error)};
  const saveCart = () => {localStorage.setItem("sem_cart",JSON.stringify(state.cart));$("cartCount").textContent=state.cart.reduce((n,x)=>n+x.quantity,0);};
  const api = async (path,options={}) => {
    const headers = {"Content-Type":"application/json",...(options.headers||{})};
    if(state.token) headers.Authorization=`Bearer ${state.token}`;
    const res=await fetch(`${cfg.API_BASE_URL}${path}`,{...options,headers});
    const type=res.headers.get("content-type")||"";
    const data=type.includes("application/json")?await res.json():await res.text();
    if(!res.ok){const e=new Error(data?.detail||data?.message||`Ошибка HTTP ${res.status}`);e.status=res.status;e.data=data;throw e;}
    return data;
  };
  const productList = data => Array.isArray(data)?data:(data.items||data.products||data.results||[]);
  const productPrice = p => Number(p.price??p.unit_price??0);
  const productIcon = category => /молоч|сыр|йогурт/i.test(category||"")?"🥛":/фрукт|овощ/i.test(category||"")?"🍎":/хлеб|выпеч/i.test(category||"")?"🥖":/напит/i.test(category||"")?"🧃":"🛍️";
  async function loadProducts(){
    const params=new URLSearchParams({limit:cfg.PAGE_SIZE,offset:state.page*cfg.PAGE_SIZE});
    if(state.query)params.set("q",state.query);if(state.category)params.set("category",state.category);
    if(state.minPrice)params.set("minPrice",state.minPrice);if(state.maxPrice)params.set("maxPrice",state.maxPrice);
    try{
      const data=await api(`${cfg.ROUTES.products}?${params}`);
      state.products=productList(data);renderProducts(data);
      showNotice("catalogMessage",state.products.length?"":"По заданным условиям товары не найдены.");
    }catch(e){state.products=[];renderProducts([]);showNotice("catalogMessage",`Не удалось загрузить каталог: ${e.message}. Проверьте доступность Gateway и Catalog Service.`,true);}
    loadCategories();
  }
  function renderProducts(data){
    const grid=$("productGrid");grid.innerHTML="";
    $("productTotal").textContent=Array.isArray(data)?"":`Найдено: ${data.total??state.products.length}`;
    state.products.forEach(p=>{
      const sku=p.sku??p.id;const card=document.createElement("article");card.className="product-card";
      card.innerHTML=`<div class="product-visual">${productIcon(p.category)}</div><span class="product-category">${esc(p.category||"Товары")}</span><h3>${esc(p.name||sku)}</h3><div class="product-bottom"><span class="price">${money(productPrice(p))}</span><button class="small-add" data-add="${esc(sku)}">+ В корзину</button></div>`;
      grid.append(card);
    });
    $("pageInfo").textContent=`Страница ${state.page+1}`;
  }
  async function loadCategories(){
    const select=$("categoryFilter"),selected=state.category;
    const cats=[...new Set(state.products.map(p=>p.category).filter(Boolean))];
    select.innerHTML='<option value="">Все категории</option>'+cats.map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join("");
    select.value=selected;
  }
  function addToCart(sku){
    const p=state.products.find(x=>String(x.sku??x.id)===String(sku));if(!p)return;
    const row=state.cart.find(x=>x.sku===String(sku));
    if(row)row.quantity++;else state.cart.push({sku:String(sku),name:p.name,price:productPrice(p),quantity:1});
    saveCart();renderCart();toast("Товар добавлен в корзину");
  }
  function renderCart(){
    saveCart();const root=$("cartItems");
    if(!state.cart.length){root.innerHTML='<p class="muted">Корзина пока пуста. Добавьте товары из каталога.</p>'; }
    else root.innerHTML=state.cart.map(x=>`<div class="cart-line"><div><h4>${esc(x.name)}</h4><span class="muted tiny">${money(x.price)} за шт.</span></div><div class="qty"><button data-qty="-1" data-sku="${esc(x.sku)}">−</button><strong>${x.quantity}</strong><button data-qty="1" data-sku="${esc(x.sku)}">+</button></div><button class="remove" data-remove="${esc(x.sku)}">Удалить</button></div>`).join("");
    const total=state.cart.reduce((n,x)=>n+x.price*x.quantity,0);$("cartSubtotal").textContent=money(total);$("cartTotal").textContent=money(total);$("checkoutButton").disabled=!state.cart.length;
  }
  async function loadOrders(target="ordersList"){
    try{const data=await api(cfg.ROUTES.orders);const rows=Array.isArray(data)?data:(data.items||data.orders||[]);
      $(target).innerHTML=rows.length?rows.map(order=>orderCard(order)).join(""):'<div class="panel muted">Заказов пока нет.</div>';
    }catch(e){$(target).innerHTML=`<div class="notice error">Не удалось получить заказы: ${esc(e.message)}. Проверьте контракт Order Service.</div>`;}
  }
  function orderCard(o){
    const id=o.id??o.order_id??"—",status=o.status??"Неизвестен",items=o.items??o.lines??[];
    return `<article class="order-card"><div class="order-head"><strong>Заказ #${esc(id)}</strong><span class="status ${/fail|cancel|reject/i.test(status)?"bad":/pending|wait|created/i.test(status)?"wait":""}">${esc(status)}</span></div><p class="muted tiny">${esc(o.created_at??o.createdAt??"")}</p><p>${items.map(i=>`${esc(i.name??i.sku)} × ${Number(i.quantity||1)}`).join(", ")}</p><strong>${money(o.total??o.total_amount??o.amount)}</strong></article>`;
  }
  async function createOrder(){
    if(!state.cart.length)return;
    const btn=$("confirmCheckout");btn.disabled=true;
    // Один ключ на одну попытку оформления. При таймауте ключ сохраняется, чтобы повтор не создал дубль.
    state.checkoutKey ||= (crypto.randomUUID?crypto.randomUUID():`${Date.now()}-${Math.random()}`);
    const payload={items:state.cart.map(x=>({sku:x.sku,quantity:x.quantity})),fulfillment_type:"PICKUP",pickup_point:$("pickupPoint").value,customer_name:$("customerName").value};
    try{
      const result=await api(cfg.ROUTES.orders,{method:"POST",headers:{"Idempotency-Key":state.checkoutKey},body:JSON.stringify(payload)});
      state.cart=[];saveCart();renderCart();$("checkoutDialog").close();state.checkoutKey=null;toast(`Заказ ${result.id??result.order_id??""} создан`);navigate("orders");loadOrders();
    }catch(e){
      if(e.status===401||e.status===403)toast("Нет доступа. Проверьте авторизацию.");
      else toast(`Заказ не подтверждён: ${e.message}. При повторе используется тот же ключ.`);
    }finally{btn.disabled=false;}
  }
  function navigate(view){
    state.view=view;document.querySelectorAll(".view").forEach(x=>x.classList.add("hidden"));$(`${view}View`).classList.remove("hidden");
    document.querySelectorAll("[data-view].nav-link").forEach(x=>x.classList.toggle("active",x.dataset.view===view));
    if(view==="cart")renderCart();if(view==="orders")loadOrders();if(view==="admin"){if(state.role!=="admin")toast("Для административного раздела нужен токен администратора");loadAdmin();}
    window.scrollTo({top:0,behavior:"smooth"});
  }
  async function loadAdmin(){
    if(state.role!=="admin"){$("adminProducts").innerHTML='<p class="muted">Войдите с ролью администратора.</p>';return;}
    try{const data=await api(cfg.ROUTES.products);const rows=productList(data);$("adminProducts").innerHTML=rows.map(p=>`<div class="admin-product"><div><strong>${esc(p.name)}</strong><small>${esc(p.sku)} · ${money(productPrice(p))}</small></div><button class="text-button" data-edit="${esc(p.sku)}">Изменить</button></div>`).join("")||'<p class="muted">Товаров нет.</p>';}
    catch(e){$("adminProducts").innerHTML=`<p class="notice error">${esc(e.message)}</p>`;}
    loadOrders("adminOrders");
  }
  function clearProductForm(){$("productForm").reset();$("editingSku").value="";$("adminSku").disabled=false;$("adminStock").value="0";$("productFormTitle").textContent="Добавить товар";}
  async function saveProduct(ev){
    ev.preventDefault();if(state.role!=="admin"){toast("Нужна роль администратора");return;}
    const sku=$("adminSku").value.trim(),editing=$("editingSku").value;
    const body={sku,name:$("adminName").value.trim(),category:$("adminCategory").value.trim(),price:Number($("adminPrice").value),currency:"RUB"};
    try{
      if(editing)await api(cfg.ROUTES.product(editing),{method:"PATCH",body:JSON.stringify(body)});
      else await api(cfg.ROUTES.products,{method:"POST",body:JSON.stringify({...body,initial_stock:Number($("adminStock").value)})});
      toast("Товар сохранён");clearProductForm();loadAdmin();loadProducts();
    }catch(e){toast(`Не удалось сохранить товар: ${e.message}`);}
  }
  async function editProduct(sku){
    try{const p=await api(cfg.ROUTES.product(sku));$("editingSku").value=p.sku;$("adminSku").value=p.sku;$("adminSku").disabled=true;$("adminName").value=p.name;$("adminCategory").value=p.category;$("adminPrice").value=p.price;$("adminStock").value=0;$("productFormTitle").textContent="Редактировать товар";window.scrollTo({top:0,behavior:"smooth"});}
    catch(e){toast(`Не удалось открыть товар: ${e.message}`);}
  }
  function bind(){
    document.addEventListener("click",e=>{
      const view=e.target.closest("[data-view]");if(view){e.preventDefault();navigate(view.dataset.view);return;}
      const add=e.target.closest("[data-add]");if(add)addToCart(add.dataset.add);
      const qty=e.target.closest("[data-qty]");if(qty){const row=state.cart.find(x=>x.sku===qty.dataset.sku);if(row){row.quantity+=Number(qty.dataset.qty);if(row.quantity<=0)state.cart=state.cart.filter(x=>x!==row);renderCart();}}
      const rem=e.target.closest("[data-remove]");if(rem){state.cart=state.cart.filter(x=>x.sku!==rem.dataset.remove);renderCart();}
      const edit=e.target.closest("[data-edit]");if(edit)editProduct(edit.dataset.edit);
    });
    let timer; $("searchInput").addEventListener("input",e=>{clearTimeout(timer);timer=setTimeout(()=>{state.query=e.target.value.trim();state.page=0;loadProducts();},300);});
    $("categoryFilter").addEventListener("change",e=>{state.category=e.target.value;state.page=0;loadProducts();});
    ["minPrice","maxPrice"].forEach(id=>$(id).addEventListener("change",e=>{state[id]=e.target.value;state.page=0;loadProducts();}));
    $("resetFilters").onclick=()=>{$("searchInput").value="";$("minPrice").value="";$("maxPrice").value="";$("categoryFilter").value="";state.query=state.category=state.minPrice=state.maxPrice="";state.page=0;loadProducts();};
    $("prevPage").onclick=()=>{if(state.page>0){state.page--;loadProducts();}};$("nextPage").onclick=()=>{state.page++;loadProducts();};
    $("refreshOrders").onclick=()=>loadOrders();$("refreshAdminProducts").onclick=()=>loadAdmin();$("refreshAdminOrders").onclick=()=>loadOrders("adminOrders");
    $("checkoutButton").onclick=()=>{if(!state.token){toast("Сначала войдите и вставьте JWT");$("loginDialog").showModal();return;}$("checkoutDialog").showModal();};
    $("checkoutForm").addEventListener("submit",e=>{e.preventDefault();createOrder();});
    $("productForm").addEventListener("submit",saveProduct);$("cancelEdit").onclick=clearProductForm;
    $("loginButton").onclick=()=>$("loginDialog").showModal();
    $("loginForm").addEventListener("submit",e=>{if(e.submitter?.id==="saveLogin"){e.preventDefault();state.token=$("tokenInput").value.trim();state.role=$("roleInput").value;localStorage.setItem("sem_token",state.token);localStorage.setItem("sem_role",state.role);$("loginDialog").close();toast("Данные входа сохранены");}});
  }
  bind();saveCart();renderCart();loadProducts();
})();